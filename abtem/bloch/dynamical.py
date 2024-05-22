from __future__ import annotations

from ast import Not
import warnings
from abc import ABCMeta, abstractmethod
from functools import partial
from typing import Iterable, Sequence

import dask.array as da
import numpy as np
from ase import Atoms
from ase.cell import Cell
from scipy.linalg import expm as expm_scipy
from scipy.spatial.transform import Rotation

from abtem.array import ArrayObject
from abtem.atoms import is_cell_orthogonal
from abtem.bloch.indexing import _find_projected_pixel_index
from abtem.bloch.utils import (
    calculate_g_vec,
    excitation_errors,
    filter_reciprocal_space_vectors,
    get_reflection_condition,
    make_hkl_grid,
    reciprocal_space_gpts,
)
from abtem.core import config
from abtem.core.axes import AxisMetadata, NonLinearAxis, ThicknessAxis
from abtem.core.backend import cp, get_array_module, validate_device
from abtem.core.chunks import equal_sized_chunks
from abtem.core.complex import abs2
from abtem.core.constants import kappa
from abtem.core.diagnostics import TqdmWrapper
from abtem.core.energy import energy2sigma, energy2wavelength
from abtem.core.ensemble import Ensemble, _wrap_with_array, unpack_blockwise_args
from abtem.core.fft import fft_interpolate, ifft2
from abtem.core.grid import Grid
from abtem.core.utils import CopyMixin, flatten_list_of_lists, get_dtype
from abtem.distributions import validate_distribution
from abtem.measurements import IndexedDiffractionPatterns
from abtem.parametrizations import validate_parametrization
from abtem.potentials.iam import PotentialArray
from abtem.waves import Waves
from scipy.cluster.hierarchy import fcluster, linkage
from scipy.spatial.distance import pdist
from abtem.core.utils import label_to_index

if cp is not None:
    from abtem.bloch.matrix_exponential import expm as expm_cupy


def calculate_scattering_factors(
    g: np.ndarray,
    atoms: Atoms,
    parametrization: str,
    k_max: float,
    thermal_sigma: float = 0.0,
    cutoff: str = "taper",
):
    """
    Calculate the scattering factors for a given set of atoms and parametrization.

    Parameters
    ----------
    g : np.ndarray
        The scattering vector lengths [1/Å].
    atoms : Atoms
        Atoms object.
    k_max : float
        Maximum scattering vector length [1/Å]. The scattering factors are set to zero for g > k_max.
    parametrization : {'lobato', 'kirkland', 'peng'}
        Parametrization for the scattering factors.
    thermal_sigma : float
        Standard deviation of the atomic displacements for the Debye-Waller factor [Å].
    cutoff : {'taper', 'hard'}
        Cutoff function for the scattering factors. 'taper' is a smooth cutoff, 'hard' is a hard cutoff.
    """

    parametrization = validate_parametrization(parametrization)

    Z_unique, Z_inverse = np.unique(atoms.numbers, return_inverse=True)
    g_unique, g_inverse = np.unique(g, return_inverse=True)

    f_e_uniq = np.zeros((Z_unique.size, g_unique.size), dtype=get_dtype(complex=True))

    if cutoff == "taper":
        T = 0.005
        alpha = 1 - 0.05
        cutoff = 1 / (1 + np.exp((g_unique / k_max - alpha) / T))
    elif cutoff == "hard":
        cutoff = g_unique <= k_max
    else:
        raise ValueError("cutoff must be 'taper' or 'hard'")

    for idx, Z in enumerate(Z_unique):
        if thermal_sigma is not None:
            DWF = np.exp(-0.5 * thermal_sigma**2 * g_unique**2 * (2 * np.pi) ** 2)
        else:
            DWF = 1.0

        scattering_factor = parametrization.scattering_factor(Z)

        f_e_uniq[idx, :] = scattering_factor(g_unique**2) * DWF

        f_e_uniq[idx, :] *= cutoff

    return f_e_uniq[np.ix_(Z_inverse, g_inverse)]


def calculate_structure_factors(
    hkl: np.ndarray,
    atoms: Atoms,
    parametrization: str,
    k_max: float,
    thermal_sigma: float = None,
    cutoff: str = "taper",
    device: str = "cpu",
):
    """
    Calculate the structure factors for a given set of atoms and parametrization.

    Parameters
    ----------
    hkl : np.ndarray
        The reciprocal space vectors as Miller indices. Given as a (N, 3) array.
    atoms : Atoms
        The Atoms object.
    parametrization : {'lobato', 'kirkland', 'peng'}
        Parametrization for the scattering factors.
    k_max : float
        Maximum scattering vector length [1/Å]. The scattering factors are set to zero for g > k_max.
    thermal_sigma : float
        Standard deviation of the atomic displacements for the Debye-Waller factor [Å].
    cutoff : {'taper', 'hard'}
        Cutoff function for the scattering factors. 'taper' is a smooth cutoff, 'hard' is a hard cutoff.
    device : {'cpu', 'gpu'}
        Device to use for calculations. Can be 'cpu' or 'gpu'.
    """
    positions = atoms.get_scaled_positions()
    g = np.linalg.norm(calculate_g_vec(hkl, atoms.cell), axis=1)

    f_e = calculate_scattering_factors(
        g=g,
        atoms=atoms,
        k_max=k_max,
        parametrization=parametrization,
        cutoff=cutoff,
        thermal_sigma=thermal_sigma,
    )

    xp = get_array_module(device)

    f_e = xp.asarray(f_e, dtype=get_dtype(complex=True))
    positions = xp.asarray(positions, dtype=get_dtype(complex=False))
    hkl = xp.asarray(hkl.T, get_dtype(complex=False))

    struct_factors = xp.sum(
        f_e * xp.exp(-2.0j * np.pi * positions[:] @ hkl),
        axis=0,
    )

    struct_factors /= atoms.cell.volume
    return struct_factors


def structure_factor_1d_to_3d(
    structure_factor: np.ndarray, hkl: np.ndarray, gpts: tuple[int, int, int]
):
    """
    Convert 1D structure factors to 3D structure factors.

    Parameters
    ----------
    structure_factor : np.ndarray
        The structure factors as a 1D array.
    hkl : np.ndarray
        The reciprocal space vectors as Miller indices as a (N, 3) array. N must be the same as the length of the structure factor.
    gpts : tuple of ints
        The number of grid points in the 3D structure factor.

    Returns
    -------
    np.ndarray
        The 3D structure factors.
    """
    xp = get_array_module(structure_factor)
    structure_factor_3d = xp.zeros(gpts, dtype=structure_factor.dtype)
    structure_factor_3d[hkl[:, 0], hkl[:, 1], hkl[:, 2]] = structure_factor
    return structure_factor_3d


def structure_factor_to_potential(
    structure_factor: np.ndarray, hkl: np.ndarray, gpts: tuple[int, int, int]
):
    """
    Calculate the potential from the structure factors.

    Parameters
    ----------
    structure_factor : np.ndarray
        The structure factors as a 1D array.
    hkl : np.ndarray
        The reciprocal space vectors as Miller indices as a (N, 3) array. N must be the same as the length of the structure factor.
    gpts : tuple of ints
        The number of grid points in the 3D structure factor.

    Returns
    -------
    np.ndarray
        The potential.
    """
    xp = get_array_module(structure_factor)
    structure_factor = structure_factor_1d_to_3d(structure_factor, hkl, gpts)
    potential = xp.fft.ifftn(structure_factor)
    potential = potential * np.prod(potential.shape) / kappa
    potential -= potential.min()
    return potential.real


def equal_slice_thicknesses(n, slice_thickness, depth):
    dz = depth / n
    n_slices = int(np.ceil(depth / slice_thickness))
    n_per_slice = equal_sized_chunks(n, n_slices)
    slice_thickness = np.array(n_per_slice) * dz
    return slice_thickness, n_per_slice


def slice_potential(
    potential_3d: np.ndarray,
    slice_chunks,
    slice_thicknesses,
    gpts=None,
    rollaxis=True,
) -> tuple[np.ndarray, np.ndarray]:

    num_slices = len(slice_chunks)
    assert num_slices == len(slice_thicknesses)

    if gpts is not None and gpts != potential_3d.shape[:2]:
        potential_3d = fft_interpolate(potential_3d, gpts + (potential_3d.shape[-1],))

    z_samplings = tuple(
        thickness / n for n, thickness in zip(slice_chunks, slice_thicknesses)
    )

    start = np.cumsum((0,) + slice_chunks)

    potential_sliced = np.stack(
        [
            np.sum(potential_3d[..., start:stop], axis=-1) * dz
            for start, stop, dz in zip(start[:-1], start[1:], z_samplings)
        ],
        axis=-1,
    )

    if rollaxis:
        potential_sliced = np.rollaxis(potential_sliced, -1)

    return potential_sliced


class BaseStructureFactor(metaclass=ABCMeta):

    def __len__(self):
        return len(self.hkl)

    @property
    def gpts(self) -> tuple[int, int, int]:
        """Number of reciprocal space grid points."""
        return reciprocal_space_gpts(self.cell, self.k_max)

    @property
    @abstractmethod
    def hkl(self):
        """The reciprocal space vectors as Miller indices."""
        pass

    @property
    @abstractmethod
    def cell(self):
        """The unit cell."""
        pass

    @property
    def g_vec(self):
        """The reciprocal space vectors."""
        return self.hkl @ self.cell.reciprocal()

    @property
    def g_vec_length(self):
        """The lengths of the reciprocal space vectors."""
        return np.linalg.norm(self.g_vec, axis=1)

    @property
    @abstractmethod
    def k_max(self):
        """The maximum scattering vector length."""
        pass

    @abstractmethod
    def get_potential_3d(self, **kwargs) -> np.ndarray:
        pass

    @abstractmethod
    def get_projected_potential(
        self,
        slice_thickness: float | Sequence[float],
        sampling: float | tuple[float, float] = None,
        gpts: int | tuple[int, int] = None,
        **kwargs,
    ) -> PotentialArray:
        pass


class StructureFactor(BaseStructureFactor):
    """
    The StructureFactors class calculates the structure factors for a given set of atoms and parametrization.

    Parameters
    ----------
    atoms : Atoms
        Atoms object.
    k_max : float
        Maximum scattering vector length [1/Å].
    parametrization : str
        Parametrization for the scattering factors.
    thermal_sigma : float
        Standard deviation of the atomic displacements for the Debye-Waller factor [Å].
    cutoff : {'taper', 'hard'}
        Cutoff function for the scattering factors. 'taper' is a smooth cutoff, 'hard' is a hard cutoff.
    device : {'cpu', 'gpu'}
        Device to use for calculations. Can be 'cpu' or 'gpu'.
    centering : {'P', 'I', 'A', 'B', 'C', 'F'}
        Lattice centering.
    """

    def __init__(
        self,
        atoms: Atoms,
        k_max: float,
        parametrization: str = "lobato",
        thermal_sigma: float = None,
        cutoff: str = "taper",
        device: str = None,
        centering: str = "P",
    ):

        self._atoms = atoms
        self._thermal_sigma = thermal_sigma

        hkl = make_hkl_grid(atoms.cell, k_max)

        if centering.lower() != "p":
            hkl = hkl[get_reflection_condition(hkl, centering)]

        self._hkl = hkl

        self._k_max = k_max

        if cutoff not in ("taper", "hard"):
            raise ValueError("cutoff must be 'taper', 'hard'")

        self._cutoff = cutoff
        self._parametrization = validate_parametrization(parametrization)
        self._device = validate_device(device)

    @property
    def atoms(self):
        return self._atoms

    @property
    def k_max(self):
        return self._k_max

    @property
    def hkl(self):
        return self._hkl

    @property
    def cell(self):
        return self.atoms.cell

    @property
    def parametrization(self):
        return self._parametrization

    def calculate_scattering_factors(self) -> np.ndarray:
        """
        Calculate the scattering factors for each atomic species in the structure.
        """
        return calculate_scattering_factors(
            self.g_vec_length,
            self.atoms,
            self._parametrization,
            self.k_max,
            self._thermal_sigma,
            self._cutoff,
        )

    def build(self, lazy: bool = True) -> StructureFactorArray:
        """
        Calculate the structure factors to obtain a StructureFactorArray object.

        Parameters
        ----------
        lazy : bool
            If True, the calculation is done lazily using dask. If False, the calculation is done eagerly.

        Returns
        -------
        StructureFactorArray
            The structure factors.
        """
        hkl = self.hkl
        if lazy:
            xp = get_array_module(self._device)
            array = da.from_array(hkl, chunks=-1).map_blocks(
                calculate_structure_factors,
                atoms=self.atoms,
                parametrization=self.parametrization,
                thermal_sigma=self._thermal_sigma,
                k_max=self.k_max,
                cutoff=self._cutoff,
                device=self._device,
                drop_axis=1,
                meta=xp.array((), dtype=get_dtype(complex=True)),
            )
        else:
            array = calculate_structure_factors(
                hkl,
                self.atoms,
                parametrization=self._parametrization,
                thermal_sigma=self._thermal_sigma,
                k_max=self.k_max,
                cutoff=self._cutoff,
                device=self._device,
            )

        return StructureFactorArray(array, self.hkl, self.atoms.cell, self.k_max)

    def get_potential_3d(self, lazy: bool = True) -> np.ndarray:
        """
        Calculate the 3D potential from the structure factors.

        Parameters
        ----------
        lazy : bool
            If True, the calculation is done lazily using dask. If False, the calculation is done eagerly.

        Returns
        -------
        np.ndarray
            The 3D potential.
        """
        return self.build(lazy=lazy).get_potential_3d()

    def get_projected_potential(
        self,
        slice_thickness: float | Sequence[float] = None,
        sampling: float | tuple[float, float] = None,
        gpts: int | tuple[int, int] = None,
        lazy: bool = True,
    ) -> PotentialArray:
        """
        Calculate the projected potential from the structure factors.

        Parameters
        ----------
        slice_thickness : float or sequence of floats
            The thickness of the slices.
        sampling : float or tuple of floats
            The sampling of the projected potential [Å].
        gpts : int or tuple of ints
            The grid points of the projected potential.
        lazy : bool
            If True, the calculation is done lazily using dask. If False, the calculation is done eagerly.

        Returns
        -------
        PotentialArray
            The projected potential.
        """
        return self.build(lazy=lazy).get_projected_potential(
            slice_thickness, sampling, gpts
        )


class StructureFactorArray(BaseStructureFactor, ArrayObject):
    """
    The StructureFactorArray class represents structure factors as an ArrayObject.

    Parameters
    ----------
    array : np.ndarray
        The structure factors as a 1D array.
    hkl : np.ndarray
        The reciprocal space vectors as Miller indices as a (N, 3) array. N must be the same as the length of the structure factor.
    cell : Cell
        The unit cell.
    k_max : float
        Maximum scattering vector length [1/Å].
    ensemble_axes_metadata : list of AxisMetadata
        Metadata for the ensemble axes.
    metadata : dict
        Metadata for the ArrayObject.
    """

    _base_dims = 1

    def __init__(
        self,
        array: np.ndarray,
        hkl: np.ndarray,
        cell: np.ndarray | Cell,
        k_max: float,
        ensemble_axes_metadata: list[AxisMetadata] = None,
        metadata: dict = None,
    ):
        if not array.shape[-1] == len(hkl):
            raise ValueError(
                "The last dimension of the array must be the same length as the number of hkl vectors"
            )

        self._hkl = hkl
        self._cell = cell
        self._k_max = k_max

        super().__init__(
            array=array,
            ensemble_axes_metadata=ensemble_axes_metadata,
            metadata=metadata,
        )

    @property
    def hkl(self):
        return self._hkl

    @property
    def cell(self):
        return self._cell

    @property
    def k_max(self):
        return self._k_max

    @property
    def gpts(self) -> tuple[int, int, int]:
        """Number of reciprocal space grid points for 3D structure factors."""
        return reciprocal_space_gpts(self.cell, self.k_max)

    def to_3d_array(self) -> np.ndarray:
        """
        Convert the 1D structure factors to 3D structure factors.

        Returns
        -------
        np.ndarray
            The 3D structure factors.
        """
        if self.is_lazy:
            xp = get_array_module(self.array)
            array = da.map_blocks(
                structure_factor_1d_to_3d,
                self.array,
                da.from_array(self.hkl, chunks=-1),
                gpts=self.gpts,
                chunks=self.gpts,
                meta=xp.array((), dtype=self.array.dtype),
            )
        else:
            array = structure_factor_1d_to_3d(self.array, self.hkl, self.gpts)
        return array

    def get_potential_3d(self) -> np.ndarray:
        """
        Calculate the 3D potential from the structure factors.

        Returns
        -------
        np.ndarray
            The 3D potential.
        """
        if self.is_lazy:
            xp = get_array_module(self.array)
            array = da.map_blocks(
                structure_factor_to_potential,
                self.array,
                da.from_array(self.hkl, chunks=-1),
                gpts=self.gpts,
                chunks=self.gpts,
                meta=xp.array((), dtype=get_dtype(complex=False)),
            )
        else:
            array = structure_factor_to_potential(self.array, self.hkl, self.gpts)
        return array

    def get_projected_potential(
        self,
        slice_thickness: float | Sequence[float] = None,
        sampling: float | tuple[float, float] = None,
        gpts: int | tuple[int, int] = None,
    ) -> PotentialArray:
        """
        Calculate the projected potential from the structure factors.

        Parameters
        ----------
        slice_thickness : float or sequence of floats
            The thickness of the slices.
        sampling : float or tuple of floats
            The sampling of the projected potential [Å].
        gpts : int or tuple of ints
            The grid points of the projected potential.
        lazy : bool
            If True, the calculation is done lazily using dask. If False, the calculation is done eagerly.

        Returns
        -------
        PotentialArray
            The projected potential.
        """
        if not is_cell_orthogonal(self.cell):
            raise NotImplementedError(
                "Converting structure factor to projected potential is not supported for non-orthogonal or rotated cells."
            )

        if sampling is not None:
            extent = np.diag(self.cell)[:2]
            grid = Grid(extent=extent, gpts=gpts, sampling=sampling)
            gpts = grid.gpts

        potential_3d = self.get_potential_3d()

        if slice_thickness is None:
            slice_thickness = self.cell[2, 2] / potential_3d.shape[-1]

        if gpts is None:
            gpts = potential_3d.shape[:2]

        slice_thickness, slice_chunks = equal_slice_thicknesses(
            n=potential_3d.shape[-1],
            slice_thickness=slice_thickness,
            depth=self.cell[2, 2],
        )

        if self.is_lazy:
            xp = get_array_module(potential_3d)
            potential_sliced = potential_3d.map_blocks(
                slice_potential,
                slice_chunks=slice_chunks,
                slice_thicknesses=slice_thickness,
                gpts=gpts,
                chunks=(len(slice_chunks),) + gpts,
                meta=xp.array((), dtype=potential_3d.dtype),
            )
        else:
            potential_sliced = slice_potential(
                potential_3d,
                slice_chunks=slice_chunks,
                slice_thicknesses=slice_thickness,
                gpts=gpts,
            )

        sampling = (
            self.cell[0, 0] / potential_sliced.shape[-2],
            self.cell[1, 1] / potential_sliced.shape[-1],
        )
        potential_array = PotentialArray(
            potential_sliced,
            slice_thickness=tuple(slice_thickness),
            sampling=sampling,
        )

        return potential_array


def calculate_M_matrix(hkl: np.ndarray, cell: Cell, energy: float) -> np.ndarray:
    """
    Calculate the M matrix for a given set of reciprocal space vectors.

    Parameters
    ----------
    hkl : np.ndarray
        The reciprocal space vectors as Miller indices. Given as a (N, 3) array.
    cell : Cell
        The unit cell.
    energy : float
        The energy of the electrons [eV].

    Returns
    -------
    np.ndarray
        The M matrix.
    """
    g = hkl @ cell.reciprocal()
    k0 = 1 / energy2wavelength(energy)
    Mii = 1 / np.sqrt(1 + g[:, 2] / k0)
    return Mii


def calculate_structure_matrix(
    hkl_selected: np.ndarray,
    cell: Cell | np.ndarray,
    energy: float,
    structure_factor: np.ndarray,
    hkl: np.ndarray,
    use_wave_eq: bool = False,
) -> np.ndarray:
    """
    Calculate the structure matrix for a given set of reciprocal space vectors.

    Parameters
    ----------
    hkl : np.ndarray
        The reciprocal space vectors as Miller indices. Given as a (N, 3) array.
    cell : Cell
        The unit cell.
    energy : float
        The energy of the electrons [eV].

    Returns
    -------
    np.ndarray
        The structure matrix.
    """
    xp = get_array_module(structure_factor)

    g = xp.asarray(calculate_g_vec(hkl_selected, cell))

    Mii = calculate_M_matrix(hkl_selected, cell, energy)
    hkl_selected = xp.asarray(hkl_selected)

    gmh = hkl_selected[None] - hkl_selected[:, None]

    structure_factor = {
        (h, k, l): value for (h, k, l), value in zip(hkl, structure_factor)
    }

    A = np.array(
        [structure_factor[(h, k, l)] for h, k, l in gmh.reshape(-1, 3)]
    ).reshape((len(hkl_selected),) * 2)

    assert np.allclose(A, A.conj().T)

    prefactor = energy2sigma(energy) / (kappa * energy2wavelength(energy) * np.pi)

    A = A * prefactor
    # A = structure_factor[gmh[..., 0], gmh[..., 1], gmh[..., 2]] #* potential_prefactor

    Mii = xp.asarray(Mii)
    #A *= Mii[None] * Mii[:, None]

    sg = xp.asarray(excitation_errors(g, energy, use_wave_eq=use_wave_eq))
    diag = 2 * 1 / energy2wavelength(energy) * sg
    #diag *= Mii

    xp.fill_diagonal(A, diag)
    return A


def calculate_dynamical_scattering(structure_matrix, hkl, cell, energy, thicknesses):
    xp = get_array_module(structure_matrix)

    Mii = xp.asarray(calculate_M_matrix(hkl, cell, energy))

    v, C = xp.linalg.eigh(A)
    gamma = v * energy2wavelength(energy) / 2.0

    np.fill_diagonal(C, np.diag(C) / Mii)

    C_inv = xp.conjugate(C.T)

    initial = np.all(hkl == [0, 0, 0], axis=1).astype(complex)
    initial = xp.asarray(initial)

    array = xp.zeros(shape=(len(thicknesses), len(hkl)), dtype=complex)

    for i, thickness in enumerate(thicknesses):
        alpha = C_inv @ initial
        array[i] = C @ (xp.exp(2.0j * xp.pi * thickness * gamma) * alpha)

    return array


def expm(A: np.ndarray) -> np.ndarray:
    """
    Calculate the matrix exponential of a given array.

    This is a device agnostic version of the scipy.linalg.expm function.

    Parameters
    ----------
    A : np.ndarray
        Input with last two dimensions are square.

    Returns
    -------
    np.ndarray
        The resulting matrix exponential with the same shape of A.
    """
    xp = get_array_module(A)

    if xp == cp:
        return expm_cupy(A)
    else:
        return expm_scipy(A)


def calculate_scattering_matrix(
    A: np.ndarray,
    hkl: np.ndarray,
    cell: Cell,
    z: float,
    energy: float,
    method: str = "expm",
) -> np.ndarray:
    """
    Calculate the scattering matrix for a given set of reciprocal space vectors.

    Parameters
    ----------
    A : np.ndarray
        The structure matrix. The last two dimensions must be square.
    hkl : np.ndarray
        The reciprocal space vectors as Miller indices. Given as a (N, 3) array.
    cell : Cell
        The unit cell.
    z : float
        The thickness of the sample [Å].
    energy : float
        The energy of the electrons [eV].
    method : {'expm', 'decomposition'}
        The method to use for calculating the scattering matrix.
            ``expm`` :
                Use a matrix exponential.
            ``decomposition`` :
                Use a Hermitian matrix eigendecomposition.

    Returns
    -------
    np.ndarray
        The scattering matrix.
    """
    xp = get_array_module(A)

    if method == "expm":
        S = expm(1.0j * xp.pi * z * A * energy2wavelength(energy))
    else:
        raise NotImplementedError("Only 'expm' method is implemented")

    Mii = calculate_M_matrix(hkl, cell, energy)
    M = xp.asarray(np.diag(Mii))
    M_inv = xp.asarray(np.diag(1 / Mii))

    S = xp.dot(M, xp.dot(S, M_inv))
    return S


def validate_k_max(k_max: float, structure_factor: BaseStructureFactor) -> float:
    """
    Check if the provided k_max is valid. If k_max is None, it is set to half the k_max of the structure factor.

    Parameters
    ----------
    k_max : float
        The maximum scattering vector length [1/Å].
    structure_factor : BaseStructureFactor
        The structure factor.

    Returns
    -------
    float
        The validated k_max.
    """
    if k_max is None:
        k_max = structure_factor.k_max / 2
    elif k_max > structure_factor.k_max / 2:
        warnings.warn(
            "provided k_max exceed half the k_max of the scattering factors, some couplings are not included"
        )
    return k_max


def exctinction_distances(structure_factor, cell, energy):
    xp = get_array_module(structure_factor)
    V = cell.volume
    return np.pi * V / (xp.abs(structure_factor) * energy2wavelength(energy) + 1e-12)


# def select_beams(
#     structure_factor,
#     energy,
#     cell,
#     max_beams=None,
#     k_max=None,
#     sg_max=None,
#     wg_max=None,
#     ignore=None,
#     criterion="sg",
# ):
#     array = structure_factor.array.copy()

#     g_vec = calculate_g_vec(structure_factor.hkl, cell)
#     g_vec_length = calculate_g_vec_length(structure_factor.hkl, cell)
#     sg = np.abs(excitation_errors(g_vec, energy))

#     if criterion == "sg":
#         values = g_vec_length * sg

#         if wg_max is not None:
#             raise ValueError("wg_max is not used when criterion is 'sg'")

#     elif criterion == "wg":
#         xig = exctinction_distances(array, energy, cell)
#         values = xig * sg

#         if wg_max is not None:
#             values[values > wg_max] = np.inf
#         elif wg_max is None and max_beams is None:
#             raise ValueError("wg_max must be provided if max_beams is not provided")
#     else:
#         raise ValueError("criterion must be 'sg' or 'wg'")

#     if ignore is not None:
#         values[ignore] = np.inf

#     if sg_max is not None:
#         values[sg > sg_max] = np.inf

#     if k_max is not None:
#         values[g_vec_length > k_max] = np.inf

#     if max_beams is not None:
#         mask = np.zeros(len(values), dtype=bool)
#         mask[np.argsort(values)[:max_beams]] = True
#     else:
#         mask = values < np.inf

#     return mask


class BlochWaves:
    """
    The BlochWaves class represents a set of Bloch waves. It may be used to calculate the dynamical diffraction patterns.

    Parameters
    ----------
    structure_factor : StructureFactor
        The structure factor.
    energy : float
        The energy of the electrons [eV].
    sg_max : float
        The maximum excitation error [1/Å].
    k_max : float
        The maximum scattering vector length [1/Å].
    orientation_matrix : np.ndarray
        An optional orientation matrix given as a (3, 3) array. If provided, the unit cell is rotated.
        Instead of providing an orientation matrix, the `.rotate` method can be used.
    centering : {'P', 'I', 'A', 'B', 'C', 'F'}
        Lattice centering.
    device : {'cpu', 'gpu'}
        Device to use for calculations. Can be 'cpu' or 'gpu'.
    use_wave_eq : bool
        If True, the Bloch wave equation derived from the wave equation is used.
        Otherwise standard Bloch wave is used.
    """

    def __init__(
        self,
        structure_factor: StructureFactor,
        energy: float,
        sg_max: float = None,
        k_max: float = None,
        orientation_matrix: np.ndarray = None,
        centering: str = "P",
        device: str = None,
        use_wave_eq: bool = False,
    ):

        cell = structure_factor.cell

        if orientation_matrix is not None:
            cell = Cell(np.dot(cell, orientation_matrix.T))

        k_max = validate_k_max(k_max, structure_factor)

        self._structure_factor = structure_factor
        self._energy = energy
        self._sg_max = sg_max
        self._k_max = k_max
        self._cell = cell
        self._centering = centering
        self._use_wave_eq = use_wave_eq
        self._device = validate_device(device)

        # g_vec = structure_factor.g_vec
        # sg = excitation_errors(g_vec, energy)

        self._hkl_mask = filter_reciprocal_space_vectors(
            hkl=structure_factor.hkl,
            cell=self._cell,
            energy=energy,
            sg_max=sg_max,
            k_max=self._k_max,
            centering=centering,
        )

        # self.set_hkl_mask(
        #     max_beams,
        #     sg_max=sg_max,
        #     wg_max=wg_max,
        #     criterion=selection_criterion,
        #     ignore=None,
        # )

    # def set_hkl_mask(
    #     self,
    #     max_beams: int = None,
    #     sg_max: float = None,
    #     wg_max: float = None,
    #     ignore: float = None,
    #     criterion: str = "wg",
    # ):
    #     if hasattr(self.structure_factor, "build"):
    #         structure_factor = self.structure_factor.build(lazy=False).to_cpu()

    #     self._hkl_mask = select_beams(
    #         structure_factor=structure_factor,
    #         energy=self.energy,
    #         cell=self.cell,
    #         max_beams=max_beams,
    #         k_max=self.k_max,
    #         wg_max=wg_max,
    #         sg_max=sg_max,
    #         ignore=ignore,
    #         criterion=criterion,
    #     )

    def __len__(self):
        return int(np.sum(self.hkl_mask))

    @property
    def hkl_mask(self):
        return self._hkl_mask

    @property
    def hkl(self):
        return self.structure_factor.hkl[self.hkl_mask]

    @property
    def g_vec(self):
        return self.hkl @ self._cell.reciprocal()

    @property
    def centering(self):
        return self._centering

    @property
    def use_wave_eq(self):
        return self._use_wave_eq

    @property
    def cell(self):
        return self._cell

    @property
    def k_max(self):
        return self._k_max

    @property
    def sg_max(self):
        return self._sg_max

    @property
    def structure_factor(self):
        return self._structure_factor

    @property
    def energy(self):
        return self._energy

    @property
    def num_bloch_waves(self):
        """The number of Bloch waves used."""
        return int(np.sum(self.hkl_mask))

    @property
    def wavelength(self):
        """The wavelength of the electrons [Å]."""
        return energy2wavelength(self.energy)

    def excitation_errors(self):
        """Excitation errors for the Bloch waves."""
        return excitation_errors(self.g_vec, self.energy)

    @property
    def structure_matrix_nbytes(self):
        """The number of bytes used by the structure matrix."""
        bytes_per_element = 128 // 8
        return self.num_beams**2 * bytes_per_element

    def get_kinematical_diffraction_pattern(
        self, excitation_error_sigma: float = None
    ) -> IndexedDiffractionPatterns:
        """
        Calculate the kinematical diffraction pattern.

        Parameters
        ----------
        excitation_error_sigma : float
            The standard deviation of the excitation errors used for weigting the structure factor intensities [1/Å].

        Returns
        -------
        IndexedDiffractionPatterns
            The kinematical diffraction pattern.
        """
        hkl = self.hkl

        S = self.structure_factor.calculate().flatten()[self.hkl_mask]
        sg = self.excitation_errors()

        S = abs2(S)

        if excitation_error_sigma is None:
            excitation_error_sigma = self._sg_max / 3.0

        intensity = S * np.exp(-(sg**2) / (2.0 * excitation_error_sigma**2))

        metadata = {"energy": self.energy, "sg_max": self._sg_max, "k_max": self.k_max}

        reciprocal_lattice_vectors = self.cell.reciprocal()

        return IndexedDiffractionPatterns(
            miller_indices=hkl,
            array=intensity,
            reciprocal_lattice_vectors=reciprocal_lattice_vectors,
            metadata=metadata,
        )

    def calculate_structure_matrix(self, lazy: bool = True) -> np.ndarray:
        """
        Calculate the structure matrix.

        Parameters
        ----------
        lazy : bool
            If True, the calculation is done lazily using dask. If False, the calculation is done eagerly.
        """
        hkl = self.hkl
        structure_factor = self.structure_factor.build(lazy=lazy)

        A = calculate_structure_matrix(
            hkl_selected=hkl,
            cell=self.cell,
            energy=self.energy,
            structure_factor=structure_factor.array,
            hkl=structure_factor.hkl,
            use_wave_eq=self.use_wave_eq,
        )
        return A

    def calculate_scattering_matrix(self, z: float) -> np.ndarray:
        """
        Calculate the scattering matrix for a given thickness.

        Parameters
        ----------
        z : float
            The thickness of the sample [Å].

        Returns
        -------
        np.ndarray
            The scattering matrix.
        """
        A = self.calculate_structure_matrix()
        hkl = self.hkl
        cell = self.cell

        xp = get_array_module(self._device)
        A = xp.asarray(A)

        S = calculate_scattering_matrix(
            A=A, hkl=hkl, cell=cell, z=z, energy=self.energy
        )
        return S

    def _calculate_array(self, thicknesses: np.ndarray):
        hkl = self.structure_factor.hkl[self.hkl_mask]
        cell = self.cell
        A = self.calculate_structure_matrix(lazy=False)

        xp = get_array_module(A)

        Mii = xp.asarray(calculate_M_matrix(hkl, cell, self.energy))

        v, C = xp.linalg.eigh(A)
        gamma = v * self.wavelength / 2.0

        np.fill_diagonal(C, np.diag(C) / Mii)

        C_inv = xp.conjugate(C.T)

        initial = np.all(hkl == [0, 0, 0], axis=1).astype(complex)
        initial = xp.asarray(initial)

        array = xp.zeros(shape=(len(thicknesses), len(hkl)), dtype=complex)

        for i, thickness in enumerate(thicknesses):
            alpha = C_inv @ initial
            array[i] = C @ (xp.exp(2.0j * xp.pi * thickness * gamma) * alpha)

        return array

    def calculate_diffraction_patterns(
        self,
        thicknesses: float | Sequence[float],
        return_complex: bool = False,
        lazy: bool = True,
        merge_tol: float = None,
    ):
        """
        Calculate the dynamical diffraction patterns for a given set of thicknesses.

        Parameters
        ----------
        thicknesses : float or sequence of floats
            The thicknesses of the sample [Å].
        return_complex : bool
            If True, the complex diffraction patterns are returned. If False, the intensity is returned. Default is False.
        lazy : bool
            If True, the calculation is done lazily using dask. If False, the calculation is done eagerly.
        merge_tol : float
            The merge tolerance for merging overlapping diffraction spots. Default is None, which means no merging is done.
        """

        thicknesses = np.array(thicknesses, dtype=float)

        if not hasattr(thicknesses, "__len__"):
            thicknesses = [thicknesses]
            ensemble_axes_metadata = []
        else:
            ensemble_axes_metadata = [
                ThicknessAxis(label="z", units="Å", values=tuple(thicknesses))
            ]

        hkl = self.structure_factor.hkl[self.hkl_mask]
        array = self._calculate_array(thicknesses)
        reciprocal_lattice_vectors = self.cell.reciprocal()

        xp = get_array_module(array)

        if merge_tol is not None:
            g_vec = self.g_vec
            clusters = fcluster(
                linkage(pdist(g_vec[:, :2]), method="complete"),
                merge_tol,
                criterion="distance",
            )

            thicknesses = xp.asarray(thicknesses)
            new_array = xp.zeros_like(array, shape=array.shape[:-1] + (clusters.max(),))
            new_hkl = np.zeros_like(hkl, shape=(clusters.max(), 3))
            for i, cluster in enumerate(label_to_index(clusters, min_label=1)):

                # new_array[:, i] = (array[:, cluster] * xp.exp(-2 * np.pi * 1.0j * g_vec[i, 2] * thicknesses)[:, None])[:, 0]
                new_array[:, i] = array[:, cluster].sum(-1)

                j = np.argmin(np.abs(excitation_errors(g_vec[cluster], self.energy)))

                new_hkl[i] = hkl[cluster][j]

            array = new_array
            hkl = new_hkl

        if len(ensemble_axes_metadata) == 0:
            array = array[0]
        else:
            reciprocal_lattice_vectors = reciprocal_lattice_vectors[None]

        if not return_complex:
            array = abs2(array)

        return IndexedDiffractionPatterns(
            miller_indices=hkl,
            array=array,
            reciprocal_lattice_vectors=reciprocal_lattice_vectors,
            ensemble_axes_metadata=ensemble_axes_metadata,
            metadata={
                "energy": self.energy,
                "sg_max": self.sg_max,
                "k_max": self.k_max,
            },
        )

    def calculate_exit_waves(
        self,
        thicknesses: float | Sequence[float],
        gpts: tuple[int, int],
        extent: tuple[float, float],
        normalization: str = "values",
        lazy: bool = True,
    ) -> Waves:
        """
        Calculate the exit waves for a given set of thicknesses.

        Parameters
        ----------
        thicknesses : float or sequence of floats
            The thicknesses of the sample [Å].
        gpts : tuple of ints
            The grid points of the exit waves.
        extent : tuple of floats
            The extent of the exit waves [Å].
        normalization : {'values', 'none'}
            # TODO
        lazy : bool
            If True, the calculation is done lazily using dask. If False, the calculation is done eagerly.

        Returns
        -------
        Waves
            The exit waves.
        """

        cell = self.cell
        g_vec = self.g_vec

        array = self._calculate_array(thicknesses)

        xp = get_array_module(array)

        # if not is_cell_orthogonal(cell):
        #     #atoms, transform = orthogonalize_cell(Atoms(cell=cell), return_transform=True)

        #     raise NotImplementedError(
        #         "Converting non-orthogonal or rotated cells to wave functions is not supported"
        #     )

        array = self._calculate_array(thicknesses)

        sampling = (1 / extent[0], 1 / extent[1])

        nm = _find_projected_pixel_index(self.g_vec, gpts, sampling)

        xp = get_array_module(array)

        thicknesses1 = xp.asarray(thicknesses)
        array2 = xp.zeros(array.shape[:-1] + gpts, dtype=array.dtype)
        for i, nmi in enumerate(nm):
            phase = xp.exp(-2 * np.pi * 1.0j * g_vec[i, 2] * thicknesses1)
            array2[..., nmi[0], nmi[1]] += array[..., i] * phase

        array = ifft2(xp.fft.ifftshift(array2, axes=(-2, -1)))
        # array = ifft2(array2)

        if normalization == "values":
            array *= np.prod(gpts)

        waves = Waves(
            array=array,
            extent=extent,
            energy=self.energy,
            ensemble_axes_metadata=[
                ThicknessAxis(label="z", units="Å", values=tuple(thicknesses))
            ],
            metadata={"normalization": "values"},
        )

        return waves

    def rotate(self, *args, degrees: bool = False):
        """
        Rotate the unit cell by a given set of Euler angles.

        Parameters
        ----------
        args : sequence of (str, float)
            The rotation axes and angles. The axes must be given as a string of 'x', 'y' or 'z',
            representing a sequence of rotation axes.
        degrees : bool
            If True, the angles are given in degrees. Default is False.

        Examples
        --------
        Rotate the unit cell by 45 degrees around the x-axis:

        >>> rotated = bloch.rotate('x', 45)
        >>> rotated
        BlochWaves
        >>> rotated.ensemble_shape
        ()

        Rotate the unit cell by 0 and 45 degrees around the x-axis. For each x-rotation, do the same around the y-axis:

        >>> rotated = bloch.rotate('x', [0, 45], 'y', [0, 45], units="deg")
        >>> rotated
        BlochWavesEnsemble
        >>> print(rotated.ensemble_shape)
        (2, 2)

        Rotate the unit cell by (0, 0) and (45, 45) degress in (x, y).

        >>> bloch.rotate('xy', [[0, 0], [45, 45]], units="deg")
        >>> rotated
        BlochWavesEnsemble
        >>> rotated.ensemble_shape
        (2,)

        Returns
        -------
        BlochWaves
            The rotated Bloch waves.
        BlochWavesEnsemble
            The rotated Bloch waves ensemble.
        """

        if not any(isinstance(arg, Iterable) for arg in args[1::2]):
            orientation_matrix = np.eye(3)
            for axes, rotation in zip(args[::2], args[1::2]):
                R = Rotation.from_euler(axes, rotation).as_matrix()
                orientation_matrix = orientation_matrix @ R

            bloch = BlochWaves(
                structure_factor=self.structure_factor,
                energy=self.energy,
                sg_max=self.sg_max,
                k_max=self.k_max,
                centering=self._centering,
                orientation_matrix=orientation_matrix,
                use_wave_eq=self.use_wave_eq,
                device=self._device,
            )

            return bloch

        ensemble = BlochwaveEnsemble(
            *args,
            structure_factor=self.structure_factor,
            energy=self.energy,
            sg_max=self.sg_max,
            k_max=self.k_max,
            centering=self._centering,
            use_wave_eq=self.use_wave_eq,
            device=self._device,
        )
        return ensemble


class BlochwaveEnsemble(Ensemble, CopyMixin):

    def __init__(
        self,
        *args,
        structure_factor: StructureFactor,
        energy: float,
        sg_max: float,
        k_max: float,
        centering: str = "P",
        device: str = None,
        use_wave_eq: bool = False,
    ):
        self._axes = args[::2]
        self._rotations = tuple(
            validate_distribution(rotation) for rotation in args[1::2]
        )

        self._structure_factor = structure_factor
        self._energy = energy
        self._centering = centering
        self._sg_max = sg_max
        self._k_max = k_max
        self._use_wave_eq = use_wave_eq
        self._device = validate_device(device)

    def get_ensemble_hkl_mask(self):
        hkl = self._structure_factor.hkl
        mask = filter_reciprocal_space_vectors(
            hkl=hkl,
            cell=self._structure_factor.atoms.cell,
            energy=self.energy,
            sg_max=self.sg_max,
            k_max=self.k_max,
            centering=self.centering,
            orientation_matrices=self.get_orientation_matrices().reshape(-1, 3, 3),
        )
        return mask

    def get_orientation_matrices(self):
        orientation_matrices = np.eye(3)
        for axes, rotation in zip(self.axes[::-1], self.rotations[::-1]):

            if hasattr(rotation, "values"):
                R = Rotation.from_euler(axes, rotation.values).as_matrix()
                R = R[(slice(None),) + (None,) * (orientation_matrices.ndim - 2)]
            else:
                R = Rotation.from_euler(axes, rotation).as_matrix()

            orientation_matrices = orientation_matrices @ R

        return orientation_matrices

    @property
    def structure_factor(self):
        return self._structure_factor

    @property
    def axes(self) -> Sequence[str]:
        return self._axes

    @property
    def rotations(self):
        return self._rotations

    @property
    def energy(self) -> float:
        return self._energy

    @property
    def centering(self) -> float:
        return self._centering

    @property
    def k_max(self) -> float:
        return self._k_max

    @property
    def use_wave_eq(self) -> bool:
        return self._use_wave_eq

    @property
    def sg_max(self) -> float:
        return self._sg_max

    @property
    def device(self) -> str:
        return self._device

    @property
    def ensemble_axes_metadata(self) -> list[NonLinearAxis]:
        ensemble_axes_metadata = []
        for axes, rotations in zip(self._axes, self.rotations):
            ensemble_axes_metadata += [
                NonLinearAxis(
                    label=f"{axes}-rotation", units="rad", values=rotations.values
                )
            ]
        return ensemble_axes_metadata

    @property
    def _ensemble_args(self):
        return tuple(
            i
            for i, rotation in enumerate(self._rotations)
            if hasattr(rotation, "__len__")
        )

    @property
    def ensemble_shape(self) -> tuple[int, ...]:
        return tuple(len(self._rotations[i]) for i in self._ensemble_args)

    def _partition_args(
        self,
        chunks: int | str | tuple[int | str | tuple[int, ...], ...] = None,
        lazy: bool = True,
    ):
        blocks = ()
        for i, n in zip(self._ensemble_args, chunks):
            blocks += (self._rotations[i].divide(n, lazy=lazy),)
        return blocks

    @property
    def _default_ensemble_chunks(self):
        return ("auto",) * len(self.ensemble_shape)

    @classmethod
    def _partial_transform(cls, *args, axes, order, num_ensemble_dims, **kwargs):

        args = unpack_blockwise_args(args)

        rotations = tuple(
            x for x, _ in sorted(zip(args, order), key=lambda pair: pair[1])
        )
        args = flatten_list_of_lists(zip(axes, rotations))

        new = cls(*args, **kwargs)
        new = _wrap_with_array(new, num_ensemble_dims)
        return new

    def _from_partitioned_args(self):
        non_ensemble_args = tuple(
            i for i in range(len(self._rotations)) if i not in self._ensemble_args
        )
        num_ensemble_dims = len(self._ensemble_args)
        order = non_ensemble_args + self._ensemble_args
        non_ensemble_args = tuple(self._rotations[i] for i in non_ensemble_args)
        kwargs = self._copy_kwargs()

        return partial(
            self._partial_transform,
            *non_ensemble_args,
            axes=self._axes,
            order=order,
            num_ensemble_dims=num_ensemble_dims,
            **kwargs,
        )

    def _calculate_diffraction_intensities(
        self,
        thicknesses: float | Sequence[float],
        return_complex: bool,
        pbar: bool,
        hkl_mask: np.ndarray = None,
    ):

        if hkl_mask is None:
            hkl_mask = self.get_ensemble_hkl_mask()

        orientation_matrices = self.get_orientation_matrices()

        shape = orientation_matrices.shape[:-2] + (
            len(thicknesses),
            hkl_mask.sum(),
        )

        pbar = TqdmWrapper(
            enabled=pbar,
            total=int(np.prod(orientation_matrices.shape[:-2])),
            leave=False,
        )

        xp = get_array_module(self.device)
        array = xp.zeros(shape, dtype=get_dtype(complex=return_complex))

        # lil_matrix((np.prod(shape[:-1]), shape[-1]))

        for i in np.ndindex(orientation_matrices.shape[:-2]):

            bw = BlochWaves(
                structure_factor=self._structure_factor,
                energy=self.energy,
                sg_max=self.sg_max,
                k_max=self.k_max,
                orientation_matrix=orientation_matrices[i],
                centering=self.centering,
                device=self.device,
                use_wave_eq=self._use_wave_eq,
            )

            # cols = np.where(bw.hkl_mask)[0]
            # rows = np.ravel_multi_index(
            #     i + (tuple(range(shape[-2])),),
            #     dims=shape[:-1],
            # )

            diffraction_patterns = bw.calculate_diffraction_patterns(
                thicknesses, return_complex=return_complex, lazy=False
            )

            array[..., bw.hkl_mask[hkl_mask]] = diffraction_patterns.array

            pbar.update_if_exists(1)

        pbar.close_if_exists()

        return array

    def _lazy_calculate_diffraction_patterns(
        self, thicknesses: float | Sequence[float], return_complex: bool, pbar: bool
    ):
        blocks = self.ensemble_blocks(1)

        def _run_calculate_diffraction_patterns(
            block, hkl_mask, thicknesses, return_complex, pbar
        ):
            block = block.item()

            array = block._calculate_diffraction_intensities(
                thicknesses=thicknesses,
                return_complex=return_complex,
                pbar=pbar,
                hkl_mask=hkl_mask,
            )

            return array

        hkl_mask = self.get_ensemble_hkl_mask()

        shape = self.ensemble_shape + (
            len(thicknesses),
            int(hkl_mask.sum()),
        )

        out_ind = tuple(range(len(shape)))

        xp = get_array_module(self.device)

        out = da.blockwise(
            _run_calculate_diffraction_patterns,
            out_ind,
            blocks,
            tuple(range(len(self.ensemble_shape))),
            da.from_array(hkl_mask),
            (-1,),
            thicknesses=thicknesses,
            return_complex=return_complex,
            new_axes={out_ind[-2]: shape[-2], out_ind[-1]: shape[-1]},
            pbar=pbar,
            concatenate=True,
            meta=xp.zeros(shape, dtype=get_dtype(complex=return_complex)),
        )
        return out, hkl_mask

    def calculate_diffraction_patterns(
        self,
        thicknesses: float | Sequence[float],
        lazy: bool = True,
        return_complex: bool = False,
        pbar: bool = None,
    ):

        if pbar is None:
            pbar = config.get("local_diagnostics.task_level_progress", False)

        if not hasattr(thicknesses, "__len__"):
            thicknesses = [thicknesses]
            ensemble_axes_metadata = []
        else:
            ensemble_axes_metadata = [
                ThicknessAxis(label="z", units="Å", values=tuple(thicknesses))
            ]

        if lazy:
            array, hkl_mask = self._lazy_calculate_diffraction_patterns(
                thicknesses=thicknesses,
                return_complex=return_complex,
                pbar=pbar,
            )
        else:
            array = self._calculate_diffraction_intensities(
                thicknesses=thicknesses,
                return_complex=return_complex,
                pbar=pbar,
            )
            hkl_mask = self.get_ensemble_hkl_mask()

        orientation_matrices = self.get_orientation_matrices()
        hkl = self._structure_factor.hkl[hkl_mask]

        reciprocal_lattice_vectors = np.matmul(
            self._structure_factor.atoms.cell.reciprocal()[None],
            np.swapaxes(orientation_matrices, -2, -1),
        )

        if not len(ensemble_axes_metadata):
            array = array[..., 0, :]
        else:
            reciprocal_lattice_vectors = reciprocal_lattice_vectors[..., None, :, :]

        result = IndexedDiffractionPatterns(
            array=array,
            miller_indices=hkl,
            reciprocal_lattice_vectors=reciprocal_lattice_vectors,
            ensemble_axes_metadata=[
                *self.ensemble_axes_metadata,
                *ensemble_axes_metadata,
            ],
            metadata={
                "label": "intensity",
                "units": "arb. unit",
                "energy": self.energy,
                "sg_max": self.sg_max,
                "k_max": self.k_max,
            },
        )

        return result
