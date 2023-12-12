"""
tests.py: Some tests for the sglportalapi package.
"""
import pickle
import re
import sys
import zipfile
from datetime import date
from pathlib import Path
from typing import List, Dict, Optional


from sglportalapi.clientside import check_session_archive
from sglportalapi.maestro import Protocol, DataFileHeader



def list_files_in_archive(zip_file_path: str) -> None:
    zip_path = Path(zip_file_path)
    with zipfile.ZipFile(zip_path, 'r') as archive:
        archive_list = archive.infolist()
        print(f"Size (bytes) : Filename")
        total_bytes = 0
        for info in archive_list:
            print(f"{info.file_size:12d} : {info.filename}")
            total_bytes += info.file_size
        print("___________________")
        print(f"{len(archive_list)} files, {total_bytes/(1024*1024):.3f} MB")


def list_protocols(zip_file_path: str) -> None:
    """
    Process all Maestro trial data files in the experiment session archive and list the unique trial protocols found
    therein.

    Args:
        zip_file_path: Full file system path locating the session archive (ZIP file).
    """
    zip_path = Path(zip_file_path)
    protocols: List[Protocol]
    file_to_proto: Dict[str, int]
    with zipfile.ZipFile(zip_path, 'r') as archive:
        protocols, file_to_proto = Protocol.extract_protocols_from_session_data(archive, set())
        print(f"Found {len(protocols)} distinct trial protocols:")
        for i, p in enumerate(protocols):
            print(f"{i:02}: {p.trial.path_name:>40}: num_reps={p.num_reps}, num_tgts={p.trial.num_targets}, "
                  f"num_segs={p.trial.num_segments}")

def verify_session_recording_date(zip_file_path: str) -> None:
    """
    Examine each Maestro data file in the specified ZIP archive and report the recording date stored in
    the file header. All data files should have the same date. If not, report the first instance of a data
    file with a different date.

    Args:
        zip_file_path: Full file system path locating the session archive (ZIP file).
    """
    zip_path = Path(zip_file_path)
    with zipfile.ZipFile(zip_path, 'r') as archive:
        data_file_name_pattern = re.compile("[.]\\d\\d\\d\\d$")
        session_date: Optional[date] = None
        archive_list = archive.infolist()
        n_files: int = 0
        for info in archive_list:
            if data_file_name_pattern.search(info.filename) is not None:
                header = DataFileHeader(archive.read(info))
                n_files += 1
                if session_date is None:
                    session_date = header.date_recorded
                elif session_date != header.date_recorded:
                    print(f"ERROR: {info.filename} recorded on a different date: "
                          f"{header.date_recorded} instead of {session_date}")
                    break
        if session_date is None:
            print(f"ERROR: No Maestro data files found in archive")
        else:
            print(f"All {n_files} Maestro data files in archive recorded on: {session_date}.")

def list_units_in_archive(zip_file_path: str) -> None:
    """
    Checks specified archive for a neural units record (pickle file) and, if present, lists the Omniplex source
    channel and number of spikes for each unit.
    """
    zip_path = Path(zip_file_path)
    with zipfile.ZipFile(zip_path, 'r') as archive:
        archive_list = archive.infolist()
        found = False
        for info in archive_list:
            if ((len(info.filename) > 7) and (info.filename[-7:].lower() == '.pickle')) or \
                    ((len(info.filename) > 4) and (info.filename[-4:].lower() == '.pkl')):
                unit_data = pickle.loads(archive.read(info))
                print(f"{len(unit_data['channel'])} neural units in pickle file:")
                for i, ch_id in enumerate(unit_data['channel']):
                    num_spikes = len(unit_data['spiketimes'][i])
                    print(f"{i:02d}: channel={ch_id}, num_spikes={num_spikes}")
                found = True
        if not found:
            print(f"==> Did not find a pickle file with neural unit info in the archive.")


def _print_usage() -> None:
    print("\nAvailable commands:\n"
          "   l <path> = List all files in the experiment session archive at <path>.\n"
          "   d <path> = Get session recording date for archive at <path>.\n"
          "   c <path> = Perform sanity check on contents of the session archive at <path>.\n"
          "   t <path> = List all unique trial protocols found in the session archive at <path>.\n"
          "   u <path> = List channel and spike train length for each neural unit found in archive at <path>.\n"
          "   h = Print this usage message.\n"
          "   q = Quit.\n\n", file=sys.stdout, flush=True)


def _process_command() -> bool:
    quit_requested = False
    command = input("[h for help] >> ")
    parts = command.split()
    if not (0 < len(parts) <= 2):
        print(f"Invalid command. Try again.\n\n")
    if parts[0] == 'h':
        _print_usage()
    elif parts[0] == 'q':
        quit_requested = True
    elif (parts[0] in ['l', 'c', 'd', 't', 'u']) and (len(parts) == 2):
        if not Path(parts[1]).is_file():
            print(f"Archive file not found: {parts[1]}\n\n")
        elif parts[0] == 'l':
            list_files_in_archive(parts[1])
            print("\n\n")
        elif parts[0] == 'c':
            err_msg = check_session_archive(parts[1])
            if len(err_msg) > 0:
                print(f"{err_msg}\n\n")
            else:
                print("Archive passed sanity checks.\n\n")
        elif parts[0] == 'd':
            verify_session_recording_date(parts[1])
            print("\n\n")
        elif parts[0] == 't':
            list_protocols(parts[1])
            print("\n\n")
        else:
            list_units_in_archive(parts[1])
            print("\n\n")
    else:
        print(f"Invalid command. Try again.\n\n")
    return quit_requested


if __name__ == '__main__':
    _print_usage()
    _done = False
    while not _done:
        _done = _process_command()

    print("\n\n...BYE!")
