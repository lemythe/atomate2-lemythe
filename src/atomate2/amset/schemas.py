"""Module defining amset document schemas."""

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Tuple, Union

import numpy as np
from monty.dev import requires
from monty.serialization import loadfn
from pydantic import BaseModel, Field
from pymatgen.core import Structure

from atomate2 import __version__
from atomate2.common.schemas.math import Matrix3D, Vector3D
from atomate2.common.schemas.structure import StructureMetadata
from atomate2.utils.datetime import datetime_str

try:
    import amset
except ImportError:
    amset = None

__all__ = ["TransportData", "MeshData", "UsageStats", "AmsetTaskDocument"]

logger = logging.getLogger(__name__)


class TransportData(BaseModel):
    """Definition of AMSET transport data model."""

    doping: List[float] = Field(None, description="Carrier concentrations in cm^-3")
    temperatures: List[float] = Field(None, description="Temperatures in K")
    fermi_levels: List[List[float]] = Field(
        None, description="Fermi level positions in eV, given as (ndoping, ntemps)"
    )
    conductivity: List[List[Matrix3D]] = Field(
        None, description="Conductivity tensor in S/m, given as (ndoping, ntemps, 3, 3)"
    )
    seebeck: List[List[Matrix3D]] = Field(
        None, description="Seebeck tensor in µV/K, given as (ndoping, ntemps, 3, 3)"
    )
    electronic_thermal_conductivity: List[List[Matrix3D]] = Field(
        None,
        description="Electronic thermal conductivity tensor in W/mK, given as "
        "(ndoping, ntemps, 3, 3)",
    )
    mobility: Dict[str, List[List[Matrix3D]]] = Field(
        None,
        description="Carrier mobility tensor in cm^2/Vs, given as "
        "{scattering_type: (ndoping, ntemps, 3, 3)}",
    )


class UsageStats(BaseModel):
    """Definition of AMSET timing data."""

    interpolation: float = Field(
        None, description="Time taken for interpolation routines (s)"
    )
    dos: float = Field(
        None, description="Time taken for density of states routines (s)"
    )
    scattering: float = Field(
        None, description="Time taken for scattering routines (s)"
    )
    transport: float = Field(None, description="Time taken for transport routines (s)")
    writing: float = Field(None, description="Time taken for io routines (s)")
    total: float = Field(None, description="Total time taken for AMSET to run (s)")
    max_memory: float = Field(None, description="Maximum memory usage (MB)")


class MeshData(BaseModel):
    """Definition of full AMSET mesh data."""

    energies: Dict[str, List[List[float]]] = Field(
        None, description="Band structure energies in eV on the irreducible mesh."
    )
    kpoints: List[Vector3D] = Field(
        None, description="K-points in fractional coordinates"
    )
    ir_kpoints: List[Vector3D] = Field(
        None, description="Irreducible k-points in fractional coordinates"
    )
    ir_to_full_kpoint_mapping: List[int] = Field(
        None, description="Mapping from irreducible to full k-points"
    )
    efermi: float = Field(None, description="Intrinsic Fermi level from band structure")
    vb_idx: Dict[str, int] = Field(
        None, description="Index of highest valence band for each spin"
    )
    num_electrons: float = Field(None, description="Number of electrons in the system")
    velocities: Dict[str, List[List[Vector3D]]] = Field(
        None, description="Band velocities for each irreducible k-point."
    )
    scattering_rates: Dict[str, List[List[List[List[List[float]]]]]] = Field(
        None,
        description="Scattering rates in s^-1, given as "
        "{spin: (nscattering_types, ndoping, ntemps, nbands, nkpoints)}",
    )
    fd_cutoffs: Tuple[List[List[float]], List[List[float]]] = Field(
        None,
        description="Energy cutoffs within which the scattering rates are calculated"
        "given as (min_cutoff, max_cutoff) where each cutoff is given"
        "as (ndoping, ntemps)",
    )


class AmsetTaskDocument(StructureMetadata):
    """Definition of VASP task document."""

    dir_name: str = Field(None, description="The directory for this AMSET task")
    last_updated: str = Field(
        default_factory=datetime_str,
        description="Timestamp for this task document was last updated",
    )
    completed_at: str = Field(
        None, description="Timestamp for when this task was completed"
    )
    input: dict = Field(None, description="The input settings")
    transport: TransportData = Field(None, description="The transport results")
    usage_stats: UsageStats = Field(None, description="Timing and memory usage")
    mesh: MeshData = Field(None, description="Full AMSET mesh data")
    converged: bool = Field(
        None, description="Whether the transport results are converged within 10 %"
    )
    kpoint_mesh: Vector3D = Field(None, description="Interpolated k-point mesh used")
    nkpoints: int = Field(None, description="Total number of interpolated k-points")
    log: str = Field(None, description="Full AMSET running log")
    is_metal: bool = Field(None, description="Whether the system is a metal")
    scattering_labels: List[str] = Field(
        None, description="The scattering types used in the calculation"
    )
    soc: bool = Field(None, description="Whether spin–orbit coupling was included")
    structure: Structure = Field(None, description="The structure used in this task")
    _schema: str = Field(
        __version__,
        description="Version of atomate2 used to create the document",
        alias="schema",
    )

    @classmethod
    @requires(amset, "amset must be installed to create an AmsetTaskDocument.")
    def from_directory(
        cls,
        dir_name: Union[Path, str],
        additional_fields: Dict[str, Any] = None,
        include_mesh: bool = False,
    ):
        """
        Create a task document from a directory containing VASP files.

        Parameters
        ----------
        dir_name
            The path to the folder containing the calculation outputs.
        additional_fields
            Dictionary of additional fields to add to output document.
        include_mesh
            Whether to include the full AMSET mesh in the document.

        Returns
        -------
        AmsetTaskDocument
            A task document for the amset calculation.
        """
        from amset.io import load_mesh
        from amset.util import cast_dict_list

        additional_fields = {} if additional_fields is None else additional_fields
        dir_name = Path(dir_name)

        settings = loadfn("settings.yaml")
        transport_file = next(Path(".").glob("*transport_*"))
        inter_mesh = re.findall(r"transport_(\d+)x(\d+)x(\d+)\.", transport_file.name)
        inter_mesh = list(map(int, inter_mesh[0]))
        log = Path("amset.log").read_text()

        transport = loadfn(transport_file)
        timing = loadfn("timing.json.gz") if Path("timing.json.gz").exists() else None

        # insert mesh if calculation is converged or convergence is not known
        mesh_kwargs = {}
        mesh_files = list(Path(".").glob("*mesh_*"))
        if len(mesh_files) > 0:
            mesh = load_mesh(mesh_files[0])
            mesh_kwargs = {
                "is_metal": mesh.pop("is_metal"),
                "scattering_labels": mesh.pop("scattering_labels"),
                "soc": mesh.pop("soc"),
            }
            if include_mesh:
                # remove duplicated data
                for k in ("doping", "temperatures", "fermi_levels", "structure"):
                    mesh.pop(k)

                mesh_kwargs["mesh"] = cast_dict_list(mesh)

        doc = cls.from_structure(
            structure=_get_structure(),
            include_structure=True,
            dir_name=dir_name,
            completed_at=datetime_str(),
            input=settings,
            transport=transport,
            usage_stats=timing,
            kpoint_mesh=inter_mesh,
            nkpoints=np.product(inter_mesh),
            log=log,
            **mesh_kwargs
        )
        doc.copy(update=additional_fields)
        return doc


def _get_structure() -> Structure:
    """Find amset input file in current directory and extract structure."""
    vr_files = list(Path(".").glob("*vasprun.xml*"))
    bs_files = list(Path(".").glob("*band_structure_data*"))

    if len(vr_files) > 0:
        from pymatgen.io.vasp import BSVasprun

        return BSVasprun(str(vr_files[0])).get_band_structure().structure
    elif len(bs_files) > 0:
        return loadfn(bs_files[0])["band_structure"].structure

    raise ValueError("Could not find amset input in current directory.")
