"""
Count secondary structures in a PDB file as determined by p-sea

https://www.biotite-python.org/apidoc/biotite.structure.annotate_sse.html
"""

# Examples:
# python ~/projects/protdiff/bin/annot_secondary_structures.py sampled_pdb/*.pdb plots/ss_cooccurrence_sampled.pdf --suffix generated --prefix "(b)"
# python ~/projects/protdiff/bin/annot_secondary_structures.py model_snapshot/training_args.json plots/ss_cooccurrence_test.pdf --suffix test --prefix "(a)"

import json
import os
import logging
import warnings
import functools
import multiprocessing as mp
import argparse
from itertools import groupby
from collections import Counter
from typing import Tuple, Collection, Literal, Dict, Any

import numpy as np
from matplotlib import pyplot as plt

import biotite.structure as struc
from biotite.application import dssp
from biotite.structure.io.pdb import PDBFile

SSE_BACKEND = Literal["dssp", "psea"]

from train import get_train_valid_test_sets


def build_datasets(training_args: Dict[str, Any]):
    """
    Build datasets given args again
    """
    # Build args based on training args
    dset_args = dict(
        timesteps=training_args["timesteps"],
        variance_schedule=training_args["variance_schedule"],
        max_seq_len=training_args["max_seq_len"],
        min_seq_len=training_args["min_seq_len"],
        var_scale=training_args["variance_scale"],
        syn_noiser=training_args["syn_noiser"],
        exhaustive_t=training_args["exhaustive_validation_t"],
        single_angle_debug=training_args["single_angle_debug"],
        single_time_debug=training_args["single_timestep_debug"],
        toy=training_args["subset"],
        angles_definitions=training_args["angles_definitions"],
        train_only=False,
    )

    train_dset, valid_dset, test_dset = get_train_valid_test_sets(**dset_args)
    logging.info(
        f"Training dset contains features: {train_dset.feature_names} - angular {train_dset.feature_is_angular}"
    )
    return train_dset, valid_dset, test_dset


def get_pdb_length(fname: str) -> int:
    """
    Get the length of the chain described in the PDB file
    """
    warnings.filterwarnings("ignore", ".*elements were guessed from atom_.*")
    structure = PDBFile.read(fname)
    if structure.get_model_count() > 1:
        return -1
    chain = structure.get_structure()[0]
    backbone = chain[struc.filter_backbone(chain)]
    l = int(len(backbone) / 3)
    return l


def count_structures_in_pdb(
    fname: str, backend: SSE_BACKEND = "psea"
) -> Tuple[int, int]:
    """Count the secondary structures (# alpha, # beta) in the given pdb file"""
    assert os.path.exists(fname)

    # Get the secondary structure
    warnings.filterwarnings("ignore", ".*elements were guessed from atom_.*")
    source = PDBFile.read(fname)
    if source.get_model_count() > 1:
        return (-1, -1)
    source_struct = source.get_structure()[0]
    chain_ids = np.unique(source_struct.chain_id)
    assert len(chain_ids) == 1
    chain_id = chain_ids[0]

    if backend == "psea":
        # a = alpha helix, b = beta sheet, c = coil
        ss = struc.annotate_sse(source_struct, chain_id)
        # https://stackoverflow.com/questions/6352425/whats-the-most-pythonic-way-to-identify-consecutive-duplicates-in-a-list
        ss_grouped = [(k, sum(1 for _ in g)) for k, g in groupby(ss)]
        ss_counts = Counter([chain for chain, _ in ss_grouped])

        num_alpha = ss_counts["a"] if "a" in ss_counts else 0
        num_beta = ss_counts["b"] if "b" in ss_counts else 0
    elif backend == "dssp":
        # https://www.biotite-python.org/apidoc/biotite.application.dssp.DsspApp.html#biotite.application.dssp.DsspApp
        app = dssp.DsspApp(source_struct)
        app.start()
        app.join()
        ss = app.get_sse()
        ss_grouped = [(k, sum(1 for _ in g)) for k, g in groupby(ss)]
        ss_counts = Counter([chain for chain, _ in ss_grouped])

        num_alpha = ss_counts["H"] if "H" in ss_counts else 0
        num_beta = ss_counts["B"] if "B" in ss_counts else 0
    else:
        raise ValueError(
            f"Unrecognized backend for calculating secondary structures: {backend}"
        )
    logging.debug(f"From {fname}:\t{num_alpha} {num_beta}")
    return num_alpha, num_beta


def make_ss_cooccurrence_plot(
    pdb_files: Collection[str],
    outpdf: str,
    max_seq_len: int = 0,
    backend: SSE_BACKEND = "psea",
    threads: int = 8,
    title_prefix: str = "",
    title_suffix: str = "",
):
    """
    Create a secondary structure co-occurrence plot
    """
    if max_seq_len > 0:
        orig_len = len(pdb_files)
        pdb_files = [p for p in pdb_files if get_pdb_length(p) <= max_seq_len]
        logging.info(
            f"Filtering out sequences with more than {max_seq_len} residues: {orig_len} --> {len(pdb_files)}"
        )
    logging.info(f"Calculating {len(pdb_files)} structures using {backend}")
    pfunc = functools.partial(count_structures_in_pdb, backend=backend)
    pool = mp.Pool(threads)
    alpha_beta_counts = list(pool.map(pfunc, pdb_files, chunksize=10))
    pool.close()
    pool.join()

    alpha_beta_counts = [p for p in alpha_beta_counts if p != (-1, -1)]
    alpha_counts, beta_counts = zip(*alpha_beta_counts)

    fig, ax = plt.subplots(dpi=300)
    h = ax.hist2d(alpha_counts, beta_counts, bins=np.arange(10), density=True)
    ax.set_xlabel(r"Number of $\alpha$ helices", fontsize=12)
    ax.set_ylabel(r"Number of $\beta$ sheets", fontsize=12)
    ax.set_title(
        f"{title_prefix} Secondary structure co-occurrence, {title_suffix}".strip(", "),
        fontsize=14,
    )
    cbar = fig.colorbar(h[-1], ax=ax)
    cbar.ax.set_ylabel("Frequency", fontsize=12)
    fig.savefig(outpdf, bbox_inches="tight")


def build_parser():
    parser = argparse.ArgumentParser(
        usage=__doc__, formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument(
        "infiles",
        type=str,
        nargs="+",
        help="PDB files to compute secondary structures for, or json file containing config for which we take test set",
    )
    parser.add_argument(
        "outpdf",
        type=str,
        help="PDF file to write plot of secondary structure co-occurrence frequencies",
    )
    parser.add_argument(
        "--backend",
        type=str,
        choices=["dssp", "psea"],
        default="psea",
        help="Backend for calculating secondary structure",
    )
    parser.add_argument(
        "-t",
        "--threads",
        type=int,
        default=mp.cpu_count(),
        help="Number of threads to use",
    )
    parser.add_argument(
        "-s", "--suffix", type=str, default="", help="Suffix for plot title"
    )
    parser.add_argument(
        "-p", "--prefix", type=str, default="", help="Prefix for plot title"
    )
    return parser


def main():
    """Run the script"""
    parser = build_parser()
    args = parser.parse_args()

    fnames = args.infiles
    is_test_data = False
    if len(fnames) == 1 and fnames[0].endswith(".json"):
        is_test_data = True
        with open(fnames[0]) as source:
            training_args = json.load(source)
        _, _, test_dset = build_datasets(training_args)
        fnames = test_dset.filenames

    make_ss_cooccurrence_plot(
        pdb_files=fnames,
        outpdf=args.outpdf,
        backend=args.backend,
        threads=args.threads,
        title_suffix=args.suffix,
        title_prefix=args.prefix,
        max_seq_len=test_dset.dset.pad if is_test_data else 0,
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()