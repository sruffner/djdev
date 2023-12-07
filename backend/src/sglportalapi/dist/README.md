# sglportalapi

Access datasets in the Lisberger lab's portal database.

## Background
The Lisberger lab data portal ('sglportal') is a distributed application used to archive experimental
data recorded in the lab in an underlying MySQL-esque MariaDB database, along with "metadata" that
help describe and organize the experimental data (behavioral and neuronal responses to visual stimuli).

Any anonymous user to the portal website can freely explore lab datasets on the home page. Filter datasets
by session, neural unit, research study, or recording date; view response plots for individual trials of the
selected session or -- under certain circumstances --, aggregate responses across repeated presentations of a
particular trial protocol. Registered users with 'download'-level access can selectively retrieve datasets from
the portal for derivate studies. Those with 'commit'-level access can also upload experimental sessions
to the database, while 'admin'-level users can add/modify metadata tables, perform user management, and examine the
contents of the portal's backup repository (hosted in an AWS S3 bucket provided by Duke IT Services).

A test version of the portal is now live [on the Duke Azure cluster](https://braincerebellumdata.dkstest.dhe.duke.edu/).
You must be inside the Duke firewall or on the VPN to access the site.

When an experiment session is committed to the portal database, the Maestro trial files
and Omniplex PL2 file(s) in the session archive are preprocessed, and the behavioral and 
neuronal responses are stored in the database for each trial presented in the session, 
along with information to reproduce trial target trajectories.

The portal implements a number of RESTful-like API endpoints to facilitate programmatic query and data retrieval from 
the underlying database. The `sglportalapi` package defines the clientside Python code and data constructs needed to 
conveniently access the API from either a Python interactive console or your own custom analysis script. With it you 
can perform tasks such as:
- Search the set of all experiment sessions archived in the portal.
- Retrieve summary information about some or all neural units recorded during a particular 
experiment session.
- Retrieve the definitions of all distinct Maestro trial protcols presented during an 
experiment.
- Retrieve trial-aligned behavioral and neuronal response data for a single trial, a 
contiguous block of trials, or all reps of a particular trial protocol during the experiment.

_**To use `sglportalapi`, you must be a registered user on the Lisberger lab portal with 'download'-level access or
better.**_ 
## Installation (for MacOS/Linux)
- Ensure that Python 3.9+ is installed on your system. We currently build the package against
version 3.9.12.
- Login to the Lisberger lab portal and navigate to the "API Client" page from the "Welcome" menu. 
You'll find this README and a CHANGELOG, along with auto-generated documentation on the key modules within the
`sglportalapi` package.
- Click on the "Download API Client" button to download the wheel file 
`sglportalapi-x.y.z-py3-none-any.whl`, where `x.y.x` is the release version number. Be sure to check the CHANGELOG for 
the package release history. As the API and portal evolves, it will be necessary to update your copy of the package!
- In a terminal console, navigate to the directory holding the wheel file you downloaded, and install 
the package: `pip install sglportalapi-x.y.z-py3-none-any.whl`.

## Usage
Construct a `PortalAccessor` object, passing the portal's URL and your registered username
and password on the portal. Then use the object's methods to search for and retrieve 
various kinds of information and data from the portal. Under the hood, `PortalAccessor` 
handles the details of verifying your identity on the portal and obtaining an access token
for future API requests, posting requests, and transforming the responses into data objects 
you can process in your own analysis code.

```python
from typing import List
from sglportalapi.clientside import PortalAccessor
from sglportalapi.data_containers import SessionInfo, NeuronInfo, TrialRep
from sglportalapi.maestro import Protocol

accessor = PortalAccessor(username='myusername', password='mypassword', url='<portal url>')

# retrieve a list of all of your experiments sessions that are stored in the portal
sessions: List[SessionInfo] = accessor.sessions(experimenter='myusername')

# retrieve a list of neurons recorded during a specified session with a signal-to-noise
# ratio of at least 5.0
neurons: List[NeuronInfo] = accessor.session_neurons(sessions[0], min_snr=5.0)

# retrieve the definitions of all trial protocols presented during the session
protocols: List[Protocol] = accessor.session_protocols(sessions[0])

# retrieve trial data for the first trial presented during the session, and include
# response data for one neural unit recorded during the session
unit_ids = [neurons[0].id]
first_trial: TrialRep = accessor.session_trial(sessions[0], trial_idx=1, unit_ids=unit_ids)

# retrieve the last 400 trial reps presented during the session
num_trials = sessions[0].number_of_trials
block: List[TrialRep] = accessor.session_trial_block(
    sessions[0], start=num_trials - 399, end=num_trials, unit_ids=unit_ids)

# retrieve trial data for all reps of a particular trial protocol
all_reps: List[TrialRep] = accessor.session_protocol_reps(
    sessions[0], protocols[0], completed=True, unit_ids=unit_ids)
```

As of version 0.4.0 of the `sglportalapi` package, it is possible to use `PortalAccessor` to commit an experiment session's 
worth of recorded data to the portal database, instead of having to upload session data through the web interface. 
The `commit_start()` function initiates a commit job on the portal server and uploads the session archive to a staging 
area in the portal's repository (housed in an Amazon Web Services S3 bucket). Upon (successful) return, the commit job
is queued for preprocessing and will run to completion provided no trial protocols presented during the session require 
manual validation (and no errors occur). If any protocol requires validation, the commit job enters a "review" phase, and
you must use the portal's web interface to finish the commit. Other `PortalAccessor` methods let you check the status of 
any commit jobs you started, cancel a pending commit job, and remove a failed or completed job from your commit history. 
**You must have "commit"-level access on the portal to use these functions.**

When you start a commit, you must provide some "metadata" describing the experiment session and any neural units recorded.
This requires knowledge of information in the so-called "metadata" tables of the portal database: username of the 
experimenter, subject ID, rig ID, name of the brain region in which any neural units were recorded, the neuron type for
each recorded unit, and the title of the research study to which the experiment belongs. You can use the `metadata_table()`
method to list the contents of these tables.

You must also supply the _experiment session archive_ (ZIP file) containing all of the information required by the 
portal:
1. All Maestro trial data files recorded during the experiment. Note that the portal only handles Maestro data files 
with version >= 19 (since Maestro 3.0.0, Sep 2012).
2. A Python pickle file containing information about any neural units recorded during the experiment; this may be 
omitted for behavior-only experiment sessions. The pickle file contains a **_single dictionary_** with the following 
keys. Each key holds a list of length `N`, where `N` is the number of identified neural units.
   - ‘channel’ (required) : The `K`-th element is the name of the Omniplex source channel on which unit K’s spikes were
   recorded - “WBn” or “SPKCn”.
   - ‘spiketimes’ (required): The `K`-th element is a 1D Numpy array holding the spike times for unit `K` in seconds 
   elapsed since the start of the electrophysiological (Omniplex) recording.
   - ‘filename’: If the archive contains multiple Omniplex PL2 files, this field is required and the `K`-th element 
   specifies the name of the PL2 source file from which spikes for unit `K` were extracted. If the archive contains a 
   single PL2 file or none at all, this can be omitted. 
   - ‘snr’: If no PL2 file is present in archive, this field is required. The `K`-th element is the estimated 
   signal-to-noise ratio for unit `K`. If the PL2 file is present, the portal automaticaly computes the unit SNR from
   the supplied spike times and the Omniplex recording on the specified channel. 
   - ‘template’: If no PL2 file is present in the archive, this field is required. The `K`-th element is a 1D Numpy 
   array holding unit `K`’s template waveform. The waveform should be 10ms long (1-ms pre, 9-ms post spike timestamp) 
   and the waveform samples should be microvolts. Again, this is automatically computed by the portal if the PL2 file
   is present.
3. The Omniplex PL2 file(s) in which neural unit activity was recorded, if available. If not, you **_must_** instead 
supply the file `timestamps.csv` containing the start times for every Maestro trial file in the archive. Each line in 
this CSV file has the form `trial_file_name.NNNN,timestamp_in_ms`. In this scenario, a trial’s “stop time” is simply the 
start time in the CSV plus the trial duration. Obviously, for behavior-only experiments, neither the PL2 file nor the
CSV file are required.
4. For experiment sessions containing pre-V21 Maestro data files, the archive must also contain the file `setnames.csv`
containing the trial set and subset corresponding to the trial recorded in each Maestro data file in the archive. Each
line in this CSV file has the form `trial_file_name.NNNN,set_name,subset_name` or `trial_file_name.NNNN,set_name` if the
trial was not part of a trial subset. 

**_DO NOT ZIP A DIRECTORY CONTAINING THESE FILES_**. The archive must not contain any directories (watch out for nasty
hidden directories, particularly __MACOSX if you're a Mac user), or the portal will gag on it.
    

The Python excerpt below shows how you might use `PortalAccessor` to upload a session archive and commit the session
data to the portal. Better yet, the package includes an interactive script to do just that: `session_uploader.py`. To 
use it, open a Terminal and run `python3.9 -m session_uploader`.

```python
from sglportalapi.clientside import PortalAccessor
from sglportalapi.data_containers import MetadataTable
from pathlib import Path
from time import sleep

accessor = PortalAccessor(username='myusername', password='mypassword', url='<portal url>')

# retrieve available subject IDs, rig IDs, brain regions, and research studies.
_, subject_table = accessor.metadata_table(MetadataTable.SUBJECTS)
_, rig_table = accessor.metadata_table(MetadataTable.RIGS)
...

# all of the 10 recorded units happen to be Purkinje cells, except the last two
unit_types = ['Purkinje cell'] * 8
unit_types.extend(['Unspecified', 'Unipolar brush cell'])

# the session archive includes all Maestro trial files, Omniplex file with neural unit recording, and a pickle file
# containing information required to preprocess the unit recordings.
zip_path = Path('/path/to/the_session_archive.zip')

# session metadata
experimenter = '<username of registered user that conducted the experiment>'
subject = '<ID of experiment subject>'
rec_date = '2022-10-24'
suffix = 1  # between 1 and 9; distinguishes multiple sessions on the same date using the same subject
rig = '<ID of rig>'
study = '<title of research study to which experiment belongs>'
notes = '<any notes particular to the session go here; can be empty string>'
brain_area = 'Flocculus'
src = 'Omniplex'
probe = '32-channel'
rate = 40000
x, y, z = 22.5, 14.8, 23.7   # probe insertion location and depth in mm

# start the commit job and then check its status every 20 seconds until it fails or completes successfully, then
# remove the job from your commit job history (unless it has entered the review phase).
ok, job_id = accessor.commit_start(zip_path, unit_types, experimenter, subject, rec_date, suffix, rig, study, notes,
                                   brain_area, src, probe, rate, x, y, z)
if not ok:
    print(f"Failed to start session commit: {job_id}")
else:
    failed, done, review_required = False, False, False
    while not done:
        ok, jobs = accessor.commit_status(job_id)
        if not ok:
            failed, done = True, True
            print(f"An error occurred while checking status of pending commit: {str(jobs)}")
        elif jobs[0]['state'] == 'REVIEW':
            done, review_required = True, True
        elif jobs[0]['state'] == 'DONE':
            done = True
        elif jobs[0]['state'] == 'FAIL':
            print(f"Commit job failed: {jobs[0]['messages'][0]}")
            failed, done = True, True
        else:
            sleep(20)

    if review_required:
        print(f"You must review and validate one or more trial protocols in the session. Use portal web interface.")
    else:
        ok, err_msg, removed = accessor.commit_remove(job_id)
        if not ok:
            print(f"Unable to remove commit job: {err_msg}")
```

## Documentation
Once the `sglportalapi` package is installed on your machine, you can use the `pydoc` command
to examine auto-generated documentation for the modules, classes, and functions defined
in the package. For example:
- `pydoc sglportalapi.clientside`  (a module)
- `pydoc sglportalapi.maestro.Protocol` (a class)
- `pydoc sglportalapi.data_containers.TrialRep.instantaneous_firing_rate` (a function)

Within the Python interactive console, this same documentation is available via the `help()` 
function, but you must import the relevant module before invoking it:
```
>>> import sglportalapi.data_containers
>>> help(sglportalapi.data_containers.SessionInfo)
```

## License
`sglportalapi` was created by [Scott Ruffner](mailto:sruffner@srscicomp.com). It is
licensed under the terms of the MIT license.

## Credits
`sglportalapi` relies on the [requests](https://docs.python-requests.org/) library to
authenticate your identity on the portal and query the portal's API endpoints. Response
data is typically represented as 1D [Numpy](https://numpy.org/) arrays.

David J Herzfeld has been instrumental in providing guidance during the development of the Lisberger lab
portal and its API, not to mention supplying sample experimental data for testing and serving as 
liaison with Duke IT. He also provided Python code for parsing the Plexon PL2 files, which is 
essential when committing experimental data to the portal.
