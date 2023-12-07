"""
tests.py: Some tests for the sglportalapi package.
"""
import zipfile
from pathlib import Path
from typing import List, Dict

from sglportalapi.clientside import check_session_archive
from sglportalapi.maestro import DataFile, Target, Protocol


def test_targets(data_file_path: str):
    with open(data_file_path, 'rb') as f:
        print(f"Loading data file at {data_file_path}: ")
        path = Path(data_file_path)
        mdf = DataFile.load(f.read(), path.name)
        print(f"Participating targets (N={len(mdf.trial.targets)}):")
        for t in mdf.trial.targets:
            print(f": {str(t)}\n")
            raw = t.to_bytes()
            print(f": As bytes = {raw}")
            t_recon = Target.from_bytes(raw)
            print(f"original target = reconstructed target?: {'yes' if t == t_recon else 'NO'}")

        print(f"Done.", flush=True)


def test_extract_protocols(zip_file_path: str):
    zip_path = Path(zip_file_path)
    protocols: List[Protocol]
    file_to_proto: Dict[str, int]
    with zipfile.ZipFile(zip_path, 'r') as archive:
        protocols, file_to_proto = Protocol.extract_protocols_from_session_data(archive, set())
        print(f"Found {len(protocols)} distinct trial protocols:")
        for i, p in enumerate(protocols):
            print(f"{i:02}: {p.trial.path_name:>40}: num_reps={p.num_reps}, num_tgts={p.trial.num_targets}, "
                  f"num_segs={p.trial.num_segments}")

    print("Done.", flush=True)


if __name__ == '__main__':
    str_zip = input('Enter full path to session archive >> ')
    err_msg = check_session_archive(str_zip)
    if len(err_msg) == 0:
        print("Archive passed sanity checks.")
    else:
        print(err_msg)