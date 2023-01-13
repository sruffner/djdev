"""
session_uploader.py: An interactive script that uploads an experiment session archive to the Lisberger portal.

The script uses the 'sglportalapi' package to upload a Maestro experiment session archive to the portal and commit
the session data to the portal database. To use it, you must be a registered portal user and have an Internet connection
to the portal website.

When the script launches, your must enter the portal's base URL and your user credentials. Once authenticated, enter the
path of the session archive, followed by the required metadata defining the session and the neuron type assigned to each
neural unit recorded during the experiment session. The script polls for this information interactively.

Once all of the required information has been supplied, the script will begin the session commit process, which can take
many minutes for a large archive (multiple GBs). Once the archive is uploaded to the portal, the script monitors
progress once every 20 seconds and posts a progress message to the console (overwriting the previous message). If an
error occurs along the way, that is reported as well.

After the session archive has been preprocessed, the portal server will immediately commit the session to the database
unless manual review of the session's trial protocols is required. Since that must be done interactively on the portal
website, the script takes no further action. If the commit job proceeds to successful completion, or if it fails at
any point for whatever reason, the script removes the commit job from the portal server.

Once a commit job is finished or fails, the script will ask you for the next session archive to commit -- so you can
commit as many sessions as you want.

**Author: saruffner.  Created: 12jan2023.**
"""
import sys
from datetime import date
from pathlib import Path
from time import time, sleep
from typing import Optional, Dict, List, Tuple

from sglportalapi.clientside import PortalAccessor
from sglportalapi.data_containers import MetadataTable


def _authenticate_with_portal() -> Optional[PortalAccessor]:
    while True:
        url = input("Enter complete URL to the portal web site > ")
        username = input("Enter your username on the portal > ")
        pwd = input("Enter your password > ")

        portal = PortalAccessor(username, pwd, url)
        emsg = portal.authenticate()
        if emsg is None:
            return portal

        print(f"\n==> Authentication failed: {emsg}\n", file=sys.stdout, flush=True)
        yes = input("Try again? (y or n) > ")
        if yes != 'y':
            return None


def _start_commit_job(portal: PortalAccessor, metadata: Dict[str, MetadataTable]) -> Tuple[bool, Optional[str]]:
    job_id: Optional[str] = None
    zip_path = _get_archive_path()
    if zip_path is None:
        return True, job_id
    unit_types = _get_neuron_types(metadata[MetadataTable.NEURON_TYPES])
    if unit_types is None:
        return True, job_id
    experimenter = input("Enter username of the experimenter ('q' to quit) > ")
    if experimenter == 'q':
        return True, job_id
    subject_id = _get_subject(metadata[MetadataTable.SUBJECTS])
    if subject_id is None:
        return True, job_id
    rec_date = _get_recording_date()
    if rec_date is None:
        return True, job_id
    suffix = _get_suffix()
    if suffix is None:
        return True, job_id
    rig_id = _get_rig(metadata[MetadataTable.RIGS])
    if rig_id is None:
        return True, job_id
    study = _get_study(metadata[MetadataTable.STUDIES])
    if study is None:
        return True, job_id
    notes = input("Enter session notes (can be empty) > ")

    # get metadata for electrophysio recording, but only if neural units were recorded. Assume Omniplex is used with
    # the 32-channel probe type and a 40KHz sampling rate.
    src, probe, rate = 'Omniplex', '32-channel', 40000
    brain_area, x, y, z = None, None, None, None
    if len(unit_types) > 0:
        brain_area = _get_brain_area(metadata[MetadataTable.BRAIN_AREAS])
        if brain_area is None:
            return True, job_id
        coords = _get_probe_coordinates()
        if coords is None:
            return True, job_id
        x, y, z = coords[0], coords[1], coords[2]

    print("\nStarting commit job...\n", file=sys.stdout, flush=True)
    ok, job_id = portal.commit_start(zip_path, unit_types, experimenter, subject_id, rec_date, suffix, rig_id, study,
                                     notes, brain_area, src, probe, rate, x, y, z, show_progress=True)
    if not ok:
        print(f"  ==> ERROR: {job_id}", file=sys.stdout, flush=True)
        return False, None
    else:
        return False, job_id


def _get_archive_path() -> Optional[Path]:
    while True:
        ans = input("Enter full path to session archive ('q' to quit) > ")
        if ans == 'q':
            return None
        p = Path(ans)
        if p.is_file():
            return p
        else:
            print("\n   ===> File not found!\n", file=sys.stdout, flush=True)


def _get_neuron_types(nt_table: MetadataTable) -> Optional[List[str]]:
    try:
        n = int(input("How many neural units were recorded during session? > "))
        if n < 0:
            return None
    except Exception:
        return None

    out: List[str] = list()
    if n > 0:
        print("\nSpecify a neuron type for each recorded unit. Available neuron types:\n", file=sys.stdout, flush=True)
        nt_table.pretty_print()
        print("\n-----------------\n", file=sys.stdout, flush=True)
        while len(out) < n:
            nt = input(f"Enter neuron type for unit {len(out) + 1} (or 'q' to quit) > ")
            if nt == 'q':
                return None
            out.append(nt)
    return out


def _get_subject(subj_table: MetadataTable) -> Optional[str]:
    print("\nAvailable experiment subjects:\n", file=sys.stdout, flush=True)
    subj_table.pretty_print()
    subj_id = input("Enter ID of experiment subject (or 'q' to quit) > ")
    return None if subj_id == 'q' else subj_id


def _get_recording_date() -> Optional[str]:
    while True:
        rec_date = input("Enter recording date in format YYYY-MM-DD (2023-01-12) (or 'q' to quit) > ")
        if rec_date == 'q':
            return None
        else:
            try:
                date.fromisoformat(rec_date)
                return rec_date
            except Exception:
                print(f"\n  ==> Invalid date string. Try again.\n", file=sys.stdout, flush=True)


def _get_suffix() -> Optional[int]:
    while True:
        ans = input("Enter session suffix in 1-9 (or 'q' to quit) > ")
        if ans == 'q':
            return None
        try:
            suffix = int(ans)
            if 1 <= suffix <= 9:
                return suffix
        except Exception:
            pass
        print("\n   ==> Invalid. Try again.\n", file=sys.stdout, flush=True)


def _get_rig(rig_table: MetadataTable) -> Optional[str]:
    print("\nAvailable experiment rigs:\n", file=sys.stdout, flush=True)
    rig_table.pretty_print()
    rig_id = input("Enter rig ID (or 'q' to quit) > ")
    return None if rig_id == 'q' else rig_id


def _get_brain_area(ba_table: MetadataTable) -> Optional[str]:
    print("\nAvailable brain area names:\n", file=sys.stdout, flush=True)
    ba_table.pretty_print()
    ba_name = input("Enter brain area name exactly (or 'q' to quit) > ")
    return None if ba_name == 'q' else ba_name


def _get_study(study_table: MetadataTable) -> Optional[str]:
    print("\nAvailable studies:\n", file=sys.stdout, flush=True)
    study_table.pretty_print()
    study_title = input("Enter study title exactly (or 'q' to quit) > ")
    return None if study_title == 'q' else study_title


def _get_probe_coordinates() -> Optional[Tuple[float, float, float]]:
    while True:
        ans = input("Enter probe coordinates X,Y,Z in mm (eg, '12.8,24,22.1') (or 'q' to quit) > ")
        if ans == 'q':
            return None
        tokens = ans.split(',')
        if len(tokens) == 3:
            try:
                x = float(tokens[0])
                y = float(tokens[1])
                z = float(tokens[2])
                if all([f >= 0 for f in [x, y, z]]):
                    return x, y, z
            except Exception:
                pass
        print("Invalid input. try again", file=sys.stdout, flush=True)


def _monitor_commit_job(job_id: str, portal: PortalAccessor) -> None:
    t0 = time()
    print("\n", file=sys.stdout, flush=True)   # make sure progress messages start on new line
    done, review_required = False, False
    erasure = "  ******************************************************************"
    while not done:
        ok, jobs = portal.commit_status(job_id)
        if not ok:
            print(f"\n   ==> ERROR while checking status of the pending commit job: {str(jobs)}\n")
            return
        elif jobs[0]['state'] == 'REVIEW':
            done, review_required = True, True
        elif jobs[0]['state'] == 'DONE':
            print(f"\nSession successfully committed to portal database!\n")
            done = True
        elif jobs[0]['state'] == 'FAIL':
            print(f"\n   ===> Commit job failed: {jobs[0]['messages'][0]}\n")
            done = True
        else:
            t_elapsed = time() - t0
            sys.stdout.write(f"\r{t_elapsed:.1f} sec: {jobs[0]['messages'][0]} {erasure}")
            sys.stdout.flush()
            sleep(5)

    if review_required:
        print(f"\n===> Preprocessing complete, but you must validate one or more trial protocols in the session. "
              f"Use portal web interface.\n")
    else:
        ok, err_msg, removed = portal.commit_remove(job_id)
        if not ok:
            print(f"Unable to remove commit job: {err_msg}")


def _run_main() -> None:
    print("session_uploader: Upload and commit experiment sessions to the Lisberger lab data portal...\n",
          file=sys.stdout, flush=True)

    portal = _authenticate_with_portal()
    if portal is None:
        return

    # get metadata for neuron types, brain areas, experiment subject, studies, and rigs
    metadata: Dict[str, MetadataTable] = dict()
    for name in [MetadataTable.NEURON_TYPES, MetadataTable.BRAIN_AREAS, MetadataTable.RIGS, MetadataTable.SUBJECTS,
                 MetadataTable.STUDIES]:
        emsg, t = portal.metadata_table(name)
        if t is None:
            print(f"\n   ===> ERROR: {emsg}\n", file=sys.stdout, flush=True)
            return None
        metadata[name] = t

    while True:
        q, job_id = _start_commit_job(portal, metadata)
        if q:
            return
        if isinstance(job_id, str):
            _monitor_commit_job(job_id, portal)

        ans = input("Commit another experiment session? (y or n) > ")
        if ans != 'y':
            return


if __name__ == '__main__':
    _run_main()
    print("\n\nBYE!", file=sys.stdout, flush=True)
    exit(0)
