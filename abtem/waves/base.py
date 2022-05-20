"""Module to describe electron waves and their propagation."""
from typing import Union, Tuple, List

import numpy as np
from ase import Atoms

from abtem.core.axes import HasAxes
from abtem.core.axes import RealSpaceAxis, FourierSpaceAxis, AxisMetadata
from abtem.core.backend import HasDevice
from abtem.core.energy import HasAcceleratorMixin
from abtem.core.grid import HasGridMixin
from abtem.potentials.potentials import Potential, AbstractPotential
from abtem.waves.tilt import HasBeamTiltMixin


def safe_floor_int(n: float, tol: int = 7):
    return int(np.floor(np.round(n, decimals=tol)))


class WavesLikeMixin(HasGridMixin, HasAcceleratorMixin, HasBeamTiltMixin, HasAxes, HasDevice):
    _base_axes = (-2, -1)
    _antialias_cutoff_gpts: Union[Tuple[int, int], None] = None

    @property
    def base_axes_metadata(self) -> List[AxisMetadata]:
        self.grid.check_is_defined()
        return [RealSpaceAxis(label='x', sampling=self.sampling[0], units='Å', endpoint=False),
                RealSpaceAxis(label='y', sampling=self.sampling[1], units='Å', endpoint=False)]

    @property
    def fourier_space_axes_metadata(self) -> List[AxisMetadata]:
        self.grid.check_is_defined()
        self.accelerator.check_is_defined()
        return [FourierSpaceAxis(label='scattering angle x', sampling=self.angular_sampling[0], units='mrad'),
                FourierSpaceAxis(label='scattering angle y', sampling=self.angular_sampling[1], units='mrad')]

    @property
    def antialias_valid_gpts(self) -> Tuple[int, int]:
        cutoff_gpts = self.antialias_cutoff_gpts
        return safe_floor_int(cutoff_gpts[0] / np.sqrt(2)), safe_floor_int(cutoff_gpts[0] / np.sqrt(2))

    @property
    def antialias_cutoff_gpts(self) -> Tuple[int, int]:
        self.grid.check_is_defined()
        if self._antialias_cutoff_gpts is not None:
            return self._antialias_cutoff_gpts

        kcut = 2. / 3. / max(self.sampling)
        extent = self.gpts[0] * self.sampling[0], self.gpts[1] * self.sampling[1]

        return safe_floor_int(kcut * extent[0]), safe_floor_int(kcut * extent[1])

    def _gpts_within_angle(self, angle: Union[None, float, str]) -> Tuple[int, int]:

        if angle is None:
            return self.gpts

        elif not isinstance(angle, str):
            return (int(2 * np.ceil(angle / self.angular_sampling[0])),
                    int(2 * np.ceil(angle / self.angular_sampling[1])))

        elif angle == 'cutoff':
            return self.antialias_cutoff_gpts

        elif angle == 'valid':
            return self.antialias_valid_gpts

        raise ValueError()

    @property
    def cutoff_angles(self) -> Tuple[float, float]:
        return (self.antialias_cutoff_gpts[0] // 2 * self.angular_sampling[0],
                self.antialias_cutoff_gpts[1] // 2 * self.angular_sampling[1])

    @property
    def rectangle_cutoff_angles(self) -> Tuple[float, float]:
        return (self.antialias_valid_gpts[0] // 2 * self.angular_sampling[0],
                self.antialias_valid_gpts[1] // 2 * self.angular_sampling[1])

    @property
    def full_cutoff_angles(self) -> Tuple[float, float]:
        return (self.gpts[0] // 2 * self.angular_sampling[0],
                self.gpts[1] // 2 * self.angular_sampling[1])

    @property
    def angular_sampling(self) -> Tuple[float, float]:
        self.accelerator.check_is_defined()
        fourier_space_sampling = self.fourier_space_sampling
        return fourier_space_sampling[0] * self.wavelength * 1e3, fourier_space_sampling[1] * self.wavelength * 1e3

    def _validate_potential(self, potential: Union[Atoms, AbstractPotential]) -> AbstractPotential:
        if isinstance(potential, Atoms):
            potential = Potential(potential)

        self.grid.match(potential)
        return potential

    def _bytes_per_wave(self) -> int:
        return 2 * 4 * np.prod(self.gpts)
