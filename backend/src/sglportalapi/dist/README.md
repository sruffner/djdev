# sglportalapi

Access datasets in the Lisberger lab's portal database.

## Background
The Lisberger lab data portal ('sglportal') is a distributed application used to archive experimental
data recorded in the lab in an underlying MySQL-esque MariaDB database, along with "metadata" that
help describe and organize the experimental data (behavioral and neuronal responses to visual stimuli).

Any anonymous user to the portal website can freely explore lab datasets on the home page. Filter datasets
by session, neural unit, research study, or recording date; view response plots for individual trials of the
selected session or -- under certain circumstances --, aggregate responses across repeated presentations of a
particular trial protocol. Registered users with 'download'-level access can selectively download datasets from
any experiment session for derivate studies. Those with 'commit'-level access can also upload experimental sessions
to the database, while 'admin'-level users can add/modify metadata tables, perform user management, and examine the
contents of the portal's backup repository (hosted in an AWS S3 bucket provided by Duke IT Services).

A test version of the portal is now live [on the Duke OpenShift cluster](https://braincerebellumdata-test.ocp.dhe.duke.edu/explore).
You must be inside the Duke firewall or on the VPN to access the site.

When an experiment session is committed to the portal database, the Maestro trial files
and Omniplex PL2 file(s) in the session archive are preprocessed, and the behavioral and 
neuronal responses are stored in the database for each trial presented in the session, 
along with information to reproduce trial target trajectories.

Downloading the trial-aligned data from the portal may prove too cumbersome, so the portal
implements a number of RESTful-like API endpoints to facilitate programmatic query and
data retrieval from the underlying database. The `sglportalapi` package defines the
clientside Python code and data constructs needed to conveniently access the API from 
either a Python interactive console or your own custom analysis script. With it you can
perform tasks such as:
- Search the set of all experiment sessions archived in the portal.
- Retrieve summary information about some or all neural units recorded during a particular 
experiment session.
- Retrieve the definitions of all distinct Maestro trial protcols presented during an 
experiment.
- Retrieve trial-aligned behavioral and neuronal response data for a single trial, a 
contiguous block of trials, or all reps of a particular trial protocol during the experiment.

_**To use `sglportalapi`, you must be a registered user on the Lisberger lab portal.**_

## Installation (for MacOS/Linux)
- Ensure that Python 3.9+ is installed on your system. We currently build the package against
version 3.9.12.
- Login to the Lisberger lab portal and navigate to the "API Client" page from the "Welcome" menu. 
You'll find this README, along with auto-generated documentation on the key modules within the
`sglportalapi` package.
- Click on the "Download API Client" button to download the wheel file 
`sglportalapi-x.y.z-py3-none-any.whl`, where `x.y.x` is the release version number.
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
