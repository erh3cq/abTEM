from functools import partial
from typing import Tuple, Union

import dask
import dask.array as da
import numpy as np
from ase import Atoms
from ase.cell import Cell
from scipy.ndimage import map_coordinates

from abtem.core.backend import copy_to_device
from abtem.core.fft import fft_crop, fft_interpolate, ifftn, fftn
from abtem.core.parametrizations import EwaldParametrization
from abtem.potentials import Potential, _PotentialBuilder
from abtem.inelastic.phonons import MDFrozenPhonons, DummyFrozenPhonons
from abtem.atoms import plane_to_axes
from abtem.core.constants import eps0


def _spatial_frequencies_orthorhombic(shape, cell: Cell):
    if not cell.orthorhombic:
        raise RuntimeError()

    kx, ky, kz = (np.fft.fftfreq(n, d=1 / n) for n in shape)
    lengths = cell.reciprocal().lengths()
    kx = kx[:, None, None] * lengths[0]
    ky = ky[None, :, None] * lengths[1]
    kz = kz[None, None, :] * lengths[2]
    return kx, ky, kz


def _spatial_frequencies_meshgrid(shape, cell):
    kx, ky, kz = (np.fft.fftfreq(n, d=1 / n) for n in shape)
    kx, ky, kz = np.meshgrid(kx, ky, kz, indexing="ij")
    kp = np.array([kx.ravel(), ky.ravel(), kz.ravel()]).T
    kx, ky, kz = np.dot(kp, cell.reciprocal().array).T
    return kx.reshape(shape), ky.reshape(shape), kz.reshape(shape)


def _spatial_frequencies(shape, cell):
    if cell.orthorhombic:
        return _spatial_frequencies_orthorhombic(shape, cell)
    else:
        return _spatial_frequencies_meshgrid(shape, cell)


def _spatial_frequencies_squared(shape, cell: Cell):
    kx, ky, kz = _spatial_frequencies(shape, cell)
    return kx ** 2 + ky ** 2 + kz ** 2


def integrate_gradient_fourier(
    array: np.ndarray, cell: Cell, in_space: str = "real", out_space: str = "real"
):
    """
    Integrate the gradient in 3d using Fourier space integration.

    Parameters
    ----------
    array : np.ndarray
    cell : ase.cell.Cell
    in_space : "real" or "fourier"
    out_space : "real" or "fourier"

    Returns
    -------

    """

    if in_space == "real":
        array = fftn(array)

    k2 = _spatial_frequencies_squared(array.shape, cell)

    k2 = 2 ** 2 * np.pi ** 2 * k2
    k2[0, 0, 0] = 1.0
    array /= k2

    if out_space == "real":
        array = ifftn(array, overwrite_x=True).real

    return array


def _superpose_deltas(positions, array, scale=1):
    corners = np.floor(positions).astype(int)
    shape = array.shape

    xi = np.array([corners[:, 0] % shape[0], (corners[:, 0] + 1) % shape[0]]).T[
        :, :, None, None
    ]
    xi = np.tile(xi, (1, 1, 2, 2)).reshape((len(positions), -1))
    yi = np.array([corners[:, 1] % shape[1], (corners[:, 1] + 1) % shape[1]]).T[
        :, None, :, None
    ]
    yi = np.tile(yi, (1, 2, 1, 2)).reshape((len(positions), -1))
    zi = np.array([corners[:, 2] % shape[2], (corners[:, 2] + 1) % shape[2]]).T[
        :, None, None, :
    ]
    zi = np.tile(zi, (1, 2, 2, 1)).reshape((len(positions), -1))

    x, y, z = (positions - corners).T
    x = np.array([1 - x, x]).T[:, :, None, None]
    y = np.array([1 - y, y]).T[:, None, :, None]
    z = np.array([1 - z, z]).T[:, None, None, :]

    values = (x * y * z).reshape((len(positions), -1)) * scale
    array[xi, yi, zi] += values
    return array


def _add_point_charges_real_space(array, atoms):
    pixel_volume = np.prod(np.diag(atoms.cell)) / np.prod(array.shape)

    inverse_cell = np.linalg.inv(np.array(atoms.cell))
    positions = np.dot(atoms.positions, inverse_cell)
    positions *= array.shape

    for number in np.unique(atoms.numbers):
        array = _superpose_deltas(
            positions[atoms.numbers == number], array, scale=number / pixel_volume,
        )

    return array


def _fourier_space_delta(kx, ky, kz, x, y, z):
    return np.exp(-2 * np.pi * 1j * (kx * x + ky * y + kz * z))


def _fourier_space_gaussian(k2, width):
    a = np.sqrt(1 / (2 * width ** 2)) / (2 * np.pi)
    return np.exp(-1 / (4 * a ** 2) * k2)


def add_point_charges_fourier(array, atoms, broadening=0.0):
    pixel_volume = np.prod(np.diag(atoms.cell)) / np.prod(array.shape)

    kx, ky, kz = _spatial_frequencies(array.shape, atoms.cell)

    if broadening:
        broadening = _fourier_space_gaussian(kx ** 2 + ky ** 2 + kz ** 2, broadening)
    else:
        broadening = 1.0

    for atom in atoms:
        scale = atom.number / pixel_volume
        array += scale * broadening * _fourier_space_delta(kx, ky, kz, *atom.position)

    return array


def _interpolate_between_cells(
    array, new_shape, old_cell, new_cell, offset=(0.0, 0.0, 0.0), order=2
):
    x = np.linspace(0, 1, new_shape[0], endpoint=False)
    y = np.linspace(0, 1, new_shape[1], endpoint=False)
    z = np.linspace(0, 1, new_shape[2], endpoint=False)

    x, y, z = np.meshgrid(x, y, z, indexing="ij")
    coordinates = np.array([x.ravel(), y.ravel(), z.ravel()]).T
    coordinates = np.dot(coordinates, new_cell) + offset

    padding = 3
    padded_array = np.pad(array, ((padding,) * 2,) * 3, mode="wrap")

    inverse_old_cell = np.linalg.inv(np.array(old_cell))
    mapped_coordinates = np.dot(coordinates, inverse_old_cell) % 1.0
    mapped_coordinates *= array.shape
    mapped_coordinates += padding

    interpolated = map_coordinates(
        padded_array, mapped_coordinates.T, mode="wrap", order=order
    )
    interpolated = interpolated.reshape(new_shape)
    return interpolated


def _interpolate_slice(array, cell, gpts, sampling, a, b):
    slice_shape = gpts + (int((b - a) / (min(sampling))),)

    slice_box = np.diag((gpts[0] * sampling[0], gpts[1] * sampling[1]) + (b - a,))

    slice_array = _interpolate_between_cells(
        array, slice_shape, cell, slice_box, (0, 0, a)
    )
    return np.trapz(slice_array, axis=-1, dx=(b - a) / (slice_shape[-1] - 1))


def _generate_slices(
    charge, ewald_potential, first_slice: int = 0, last_slice: int = None
):
    if last_slice is None:
        last_slice = len(ewald_potential)

    if ewald_potential.plane != "xy":
        axes = plane_to_axes(ewald_potential.plane)
        charge = np.moveaxis(charge, axes[:2], (0, 1))
        atoms = ewald_potential._transformed_atoms()
    else:
        atoms = ewald_potential.frozen_phonons.atoms

    atoms = ewald_potential.frozen_phonons.randomize(atoms)

    charge = -fftn(charge, overwrite_x=True)

    charge = fft_crop(
        charge, charge.shape[:2] + (ewald_potential.num_slices,), normalize=True
    )

    charge = add_point_charges_fourier(
        charge, atoms, ewald_potential.integrator.parametrization.width
    )

    potential = (
        integrate_gradient_fourier(
            charge, atoms.cell, in_space="fourier", out_space="real"
        )
        / eps0
    )

    for i, ((a, b), slic) in enumerate(
        zip(
            ewald_potential.slice_limits[first_slice:last_slice],
            ewald_potential.generate_slices(first_slice, last_slice),
        )
    ):
        slice_array = _interpolate_slice(
            potential, atoms.cell, ewald_potential.gpts, ewald_potential.sampling, a, b
        )

        slic._array = slic._array + copy_to_device(slice_array[None], slic.array)

        slic._array -= slic._array.min()
        yield slic


class ChargeDensityPotential(_PotentialBuilder):
    """
    The charge density potential is used to calculate the electrostatic potential from a set of core charges defined by
    an ASE Atoms and corresponding electron charge density defined by a NumPy array.

    Parameters
    ----------
    atoms : Atoms or FrozenPhonons
        Atoms or FrozenPhonons defining the atomic configuration(s) used in the independent atom model for calculating
        the electrostatic potential(s).
    charge_density : 3d array
        Charge density as a 3d NumPy array [electrons / Å^3].
    gpts : one or two int, optional
        Number of grid points in x and y describing each slice of the potential. Provide either "sampling" or "gpts".
    sampling : one or two float, optional
        Sampling of the potential in x and y [1 / Å]. Provide either "sampling" or "gpts".
    slice_thickness : float or sequence of float, optional
        Thickness of the potential slices in Å. If given as a float the number of slices are calculated by dividing the
        slice thickness into the z-height of supercell.
        The slice thickness may be given as a sequence of values for each slice, in which case an error will be thrown
        if the sum of slice thicknesses is not equal to the height of the atoms.
        Default is 0.5 Å.
    exit_planes : int or tuple of int, optional
        The `exit_planes` argument can be used to calculate thickness series.
        Providing `exit_planes` as a tuple of int indicates that the tuple contains the slice indices after which an
        exit plane is desired, and hence during a multislice simulation a measurement is created. If `exit_planes` is
        an integer a measurement will be collected every `exit_planes` number of slices.
    plane : str or two tuples of three float, optional
        The plane relative to the provided Atoms mapped to xy plane of the Potential, i.e. provided plane is
        perpendicular to the propagation direction. If str, it must be a combination of two of 'x', 'y' and 'z',
        the default value 'xy' indicates that potential slices are cuts the 'xy'-plane of the Atoms.
        The plane may also be specified with two arbitrary 3d vectors, which are mapped to the x and y directions of
        the potential, respectively. The length of the vectors has influence. If the vectors are not perpendicular,
        the second vector is rotated in the plane to become perpendicular. Providing a value of
        ((1., 0., 0.), (0., 1., 0.)) is equivalent to providing 'xy'.
    origin : three float, optional
        The origin relative to the provided Atoms mapped to the origin of the Potential. This is equivalent to shifting
        the atoms
        The default is (0., 0., 0.).
    box : three float, optional
        The extent of the potential in x, y and z. If not given this is determined from the Atoms. If the box size does
        not match an integer number of the atoms' supercell, an affine transformation may be necessary to preserve
        periodicity, determined by the `periodic` keyword.
    periodic : bool, True
        If a transformation of the atomic structure is required, `periodic` determines how the atomic structure is
        transformed. If True, the periodicity of the Atoms is preserved, which may require applying a small affine
        transformation to the atoms. If False, the transformed potential is effectively cut out of a larger repeated
        potential, which may not preserve periodicity.
    device : str, optional
        The device used for calculating the potential. The default is determined by the user configuration file.

    """

    def __init__(
        self,
        atoms: Union[Atoms, MDFrozenPhonons],
        charge_density: np.ndarray = None,
        gpts: Union[int, Tuple[int, int]] = None,
        sampling: Union[float, Tuple[float, float]] = None,
        slice_thickness: Union[float, Tuple[float]] = 0.5,
        plane: str = "xy",
        box: Tuple[float, float, float] = None,
        origin: Tuple[float, float, float] = (0.0, 0.0, 0.0),
        periodic: bool = True,
        exit_planes: int = None,
        device: str = None,
    ):

        if hasattr(atoms, "randomize"):
            self._frozen_phonons = atoms
        else:
            self._frozen_phonons = DummyFrozenPhonons(atoms)

        self._charge_density = charge_density.astype(np.float32)

        super().__init__(
            gpts=gpts,
            sampling=sampling,
            cell=atoms.cell,
            slice_thickness=slice_thickness,
            exit_planes=exit_planes,
            device=device,
            plane=plane,
            origin=origin,
            box=box,
            periodic=periodic,
        )

    @property
    def frozen_phonons(self):
        return self._frozen_phonons

    @property
    def num_frozen_phonons(self):
        return len(self.frozen_phonons)

    @property
    def ensemble_axes_metadata(self):
        return self.frozen_phonons.ensemble_axes_metadata

    @property
    def ensemble_shape(self) -> Tuple[int, ...]:
        return self.frozen_phonons.ensemble_shape

    @property
    def is_lazy(self):
        return isinstance(self.charge_density, da.core.Array)

    @property
    def charge_density(self):
        return self._charge_density

    @staticmethod
    def _wrap_charge_density(charge_density, frozen_phonon):
        return np.array(
            [{"charge_density": charge_density, "atoms": frozen_phonon}], dtype=object
        )

    def _partition_args(self, chunks: int = 1, lazy: bool = True):

        chunks = self._validate_chunks(chunks)

        charge_densities = self.charge_density

        if len(charge_densities.shape) == 3:
            charge_densities = charge_densities[None]
        elif len(charge_densities.shape) != 4:
            raise RuntimeError()

        if len(self.ensemble_shape) == 0:
            blocks = np.zeros((1,), dtype=object)
        else:
            blocks = np.zeros((len(chunks[0]),), dtype=object)

        if lazy:
            if not isinstance(charge_densities, da.core.Array):
                charge_densities = da.from_array(
                    charge_densities, chunks=(1, -1, -1, -1)
                )

            if charge_densities.shape[0] != self.ensemble_shape:
                charge_densities = da.tile(
                    charge_densities, self.ensemble_shape + (1, 1, 1)
                )

            charge_densities = charge_densities.to_delayed()

        elif hasattr(charge_densities, "compute"):
            raise RuntimeError

        frozen_phonon_blocks = self._ewald_potential().frozen_phonons._partition_args(
            lazy=lazy
        )[0]

        for i, (charge_density, frozen_phonon) in enumerate(
            zip(charge_densities, frozen_phonon_blocks)
        ):

            if lazy:
                block = dask.delayed(self._wrap_charge_density)(
                    charge_density.item(), frozen_phonon
                )
                blocks.itemset(i, da.from_delayed(block, shape=(1,), dtype=object))

            else:
                blocks.itemset(
                    i, self._wrap_charge_density(charge_density, frozen_phonon)
                )

        if lazy:
            blocks = da.concatenate(list(blocks))

        return (blocks,)

    @staticmethod
    def _charge_density_potential(*args, frozen_phonons_partial, **kwargs):
        args = args[0]
        if hasattr(args, "item"):
            args = args.item()

        args["atoms"] = frozen_phonons_partial(args["atoms"])

        kwargs.update(args)
        potential = ChargeDensityPotential(**kwargs)
        return potential

    def _from_partitioned_args(self):
        kwargs = self._copy_kwargs(
            exclude=("atoms", "charge_density"), cls=ChargeDensityPotential
        )
        frozen_phonons_partial = (
            self._ewald_potential().frozen_phonons._from_partitioned_args()
        )

        return partial(
            self._charge_density_potential,
            frozen_phonons_partial=frozen_phonons_partial,
            **kwargs
        )

    def _interpolate_slice(self, array, cell, a, b):
        slice_shape = self.gpts + (int((b - a) / min(self.sampling)),)

        slice_box = np.diag(self.box[:2] + (b - a,))

        slice_array = _interpolate_between_cells(
            array, slice_shape, cell, slice_box, (0, 0, a)
        )

        return np.trapz(slice_array, axis=-1, dx=(b - a) / (slice_shape[-1] - 1))

    def _integrate_slice(self, array, a, b):
        dz = self.box[2] / array.shape[2]
        na = int(np.floor(a / dz))
        nb = int(np.floor(b / dz))
        slice_array = np.trapz(array[..., na:nb], axis=-1, dx=(b - a) / (nb - na - 1))
        return fft_interpolate(slice_array, new_shape=self.gpts, normalization="values")

    def _ewald_potential(self):
        ewald_parametrization = EwaldParametrization(width=1)

        return Potential(
            atoms=self.frozen_phonons,
            gpts=self.gpts,
            sampling=self.sampling,
            parametrization=ewald_parametrization,
            slice_thickness=self.slice_thickness,
            projection="finite",
            plane=self.plane,
            box=self.box,
            origin=self.origin,
            exit_planes=self.exit_planes,
            device=self.device,
        )

    def generate_slices(self, first_slice: int = 0, last_slice: int = None):

        if last_slice is None:
            last_slice = len(self)

        if len(self.charge_density.shape) == 4:
            if self.charge_density.shape[0] > 1:
                raise RuntimeError()

            array = self.charge_density[0]
        elif len(self.charge_density.shape) == 3:
            array = self.charge_density
        else:
            raise RuntimeError()

        ewald_potential = self._ewald_potential()

        for slic in _generate_slices(
            array, ewald_potential, first_slice=first_slice, last_slice=last_slice
        ):
            yield slic
