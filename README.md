# Lisberger Lab Data Portal

A web application for archiving, viewing, and sharing neuroscience data collected in Dr. Stephen G. Lisberger's
laboratory at Duke University.

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
- Commmit an experiment session's worth of data to the portal database (if you have 'commit' access).

_**To use `sglportalapi`, you must be a registered user on the Lisberger lab portal with 'download'-level access or
better.**_ More details on installing and using the `sglportalapi` package are available in the package README
(see backend/src/sglportalapi/dist/README).

## Deployment

The portal is a distributed "cloud" application deployed on a [Kubernetes](https://kubernetes.io/) cluster. An earlier 
incarnation of the portal was deployed on a RedHat OpenShift cluster, but Duke is in the process of transitioning its 
cloud services to Microsoft [Azure](https://azure.microsoft.com/). Two distinct Azure clusters are available: `dkstest` 
for testing/debugging apps and `dks` for the production versions. Currently, the portal is only deployed to the test
cluster, in the `braincerebellumdata` namespace.

The application now consists of 3 interdependent deployments:
1. `data-store`: A single-pod deployment with the application's two persistence mechanisms: the MariaDB server and its
underlying database files; and an in-memory Redis server instance for Redis worker queueing and temporary storage of 
information during a long-running experiment commit workflow. 
2. `web-server`: A 2-replica deployment of the GUnicorn-based Python/Flask/Dash web backend.
3. `web-worker`: A 2-replica deployment of the Redis Queue workers that handle background tasks for the experiment
session commit workflow.

The web-server backend replicas use Redis to store information on in-progress session commit jobs (so that the backend
itself remains stateless), and the RQ workers handle various background tasks that are queued by the web server
because they require extensive processing time (preprocessing a session archive; committing a session to the database
and saving it to the backup repository; backing up the various operational logs).

Two persistent volume claims are allocated to the deployed application:
1. `pvc-db`: A 200GB volume for exclusive use by the `data-store` pod. All the MariaDB database files are maintained on
this volume.
2. `pvc-backend`: A 100GB volume attacheed to the `web-server` and `web-worker` pods. Several log files -- the critical
database operations log, an API request history, and the application messages log -- are maintained here, as well as a
staging are used when a session archive is being actively preprocessed and/or committed to the portal database.

For addition details on how the portal application is deployed, see `deploy-dkstest-braincerebellumdat.yaml`.

## Structure of experiment data archives

When you commit an experiment's worth of behavioral and neural response data to the portal, you must supply various
metadata: the recording date, the experimenter, the subject, and other information. If you commit the experiment session
interactively through the portal, the web interface displays the various fields that must be "filled in" before 
proceeding with the commit. In addition, you must prepare an_experiment session data archive_ (ZIP file) containing all
of the data files required by the portal:
1. All Maestro trial data files recorded during the experiment. Note that the portal only handles Maestro data files 
with version >= 21 (since Maestro 4.0.0, Nov 2018).
2. A Python pickle file containing information about any neural units recorded during the experiment; this may be 
omitted for behavior-only experiment sessions. The pickle file contains a **_single dictionary_** with the following 
keys. Each key holds a list of length `N`, where `N` is the number of identified neural units.
   - `channel` (required) : The `K`-th element is the name of the Omniplex source channel on which unit K’s spikes were
   recorded - “WBn” or “SPKCn”.
   - `spiketimes` (required): The `K`-th element is a 1D Numpy array (`float64` data type) holding the spike times for 
   unit `K` in seconds elapsed since the start of the electrophysiological (Omniplex) recording.
   - `filename`: If the archive contains multiple Omniplex PL2 files (rare), this field is required and the `K`-th 
   element is the name of the PL2 source file from which spikes for unit `K` were extracted. If the archive contains a 
   single PL2 file or none at all, this can be omitted. 
   - `snr`: If no PL2 file is present in archive, this field is required. The `K`-th element is the estimated 
   signal-to-noise ratio for unit `K`. If the PL2 file is present, the portal automaticaly computes the unit SNR from
   the supplied spike times and the Omniplex recording on the specified channel. 
   - `template`: If no PL2 file is present in the archive, this field is required. The `K`-th element is a 1D Numpy 
   array (`float64` data type) holding the template waveform for unit `K`. The waveform should be 10ms long (1-ms pre, 
   9-ms post spike timestamp) and the waveform samples should be microvolts. Again, this is automatically computed by 
   the portal if the PL2 file is present.
3. The Omniplex PL2 file(s) in which neural unit activity was recorded, if available. If not, you **_must_** instead 
supply the file `timestamps.csv` containing the start times for every Maestro trial file in the archive. Each line in 
this CSV has the form `trial_file_name.NNNN,timestamp_in_ms`, where the timestamps are time elapsed (in milliseconds) 
since the start of the electrode recording (presumably on the Omniplex system, though theoretically this could be some 
other system for timestamping spikes on multiple neural units). In this scenario, a trial’s “stop time” is simply the 
start time in the CSV plus the trial duration. Without this trial timing information, it is not possible to extract the
spike train for each neural unit during each trial. Obviously, for behavior-only experiments, neither the PL2 file nor 
the CSV file are required.
4. For experiment sessions containing pre-V21 Maestro data files, the archive must also contain the file `setnames.csv`
containing the trial set and subset corresponding to the trial recorded in each Maestro data file in the archive. Each
line in this CSV file has the form `trial_file_name.NNNN,set_name,subset_name` or `trial_file_name.NNNN,set_name` if the
trial was not part of a trial subset.

**_DO NOT ZIP A DIRECTORY CONTAINING THESE FILES_**. The archive must not contain any directories (watch out for nasty
hidden directories, particularly __MACOSX if you're a Mac user), or the portal will gag on it.

## License
This application was created by [Scott Ruffner](mailto:sruffner@srscicomp.com). It is licensed under the terms of the MIT license.

## Credits
In addition to the Python standard library, the portal application relies on a number of other open-source libraries.
It uses the [Dash](https://dash.plotly.com/) framework along with [Dash Bootstrap](https://dash-bootstrap-components.opensource.faculty.ai/docs/components/alert/) 
to implement a [Flask](https://flask.palletsprojects.com/en/3.0.x/)-based web interface in pure Python, and takes 
advantage of the [DataJoint](https://datajoint.com/docs/) framework to define, populate, and query the underlying 
[MariaDB](https://mariadb.org/) database with the lab's datasets. When deployed to the Azure cluster, the Dash backend 
replicas run on a production-ready [GUnicorn](https://gunicorn.org/) server.

The [Plotly library](https://plotly.com/python/) generates the graphical plots displayed in the portal, and various data
analyses are performed with the help of the [Numpy](https://numpy.org/) and [SciPy](https://scipy.org/) libraries. The 
`sglportalapi` clientside Python package relies on [requests](https://docs.python-requests.org/) library to authenticate your identity on the 
portal and query the portal's API endpoints. Response data is typically represented as Numpy arrays.

All experiment data committed to the portal are also archived to a backup repository in a dedicated Amazon Web Services
S3 bucket using the [Boto3](https://aws.amazon.com/sdk-for-python/) library. Persistent state on the portal server is 
managed using a [Redis](https://redis.com/) server, and long-running background tasks are executed via 
[RQ](https://python-rq.org/) workers.

Many thanks to David J Herzfeld, who has been instrumental in providing guidance during the development of the 
portal and its API, not to mention supplying sample experimental data for testing and serving as liaison with Duke IT. 
He also provided Python code for parsing the Plexon PL2 files, which is essential when committing experimental data to 
the portal.
