"""
sgl_schema.py: A database schema for the Lisberger laboratory.

This module defines all of the DataJoint classes (aka database tables) comprising a common framework pipeline for the
Lisberger laboratory database. The primary purpose of this pipeline is to "digest" raw data from experiment sessions,
storing the behavioral and neural data, along with stimulus protocols and other important metadata in a logical
structure that will facilitate finding/selecting specific data collections within the database.

The "raw data repository" is a file system accessible to the DataJoint pipeline. The repository serves a two-fold
purpose:
    1) It provides an archive for the original raw data files from the lab's experiments. The plan is to backup this
    repository to cloud-based storage on a regular basis.
    2) The DJ pipeline accesses the raw files in the repository when "digesting" an experiment session and storing the
    collected data and metadata in the lab database. If there is a need for data not kept in the database -- like
    the original high-resolution extracellular voltage recording for a given neural unit -- that data can be retrieved
    (albeit more slowly than via the pipeline). Furthermore, the directory structure of the repository, coupled
    with metadata files saved therein, serve as a backup for the database itself. In the event that the lab database
    becomes corrupted and recovery is not possible, it can be rebuilt from scratch -- without user input -- by
    digesting the contents of the entire repository.

Here is the prescribed layout for the repository's directory structure. This has changed over the course of early
development efforts
    $DATA_ROOT
        /logs
            This directory will contain log-style files that encapsulate all changes made to the lab database since
            the database was created. To reconstruct the database, a script would digest these log files in
            chronological order, performing the operations defined therein.
        /staging
            This is a scratch directory that the backend uses to store information during an active session commit,
            which is a multi-step, user-interactive procedure. When a commit is initiated, a temporary directory is
            created to hold the uploaded session data archive, and some other files. Once the commit is fully executed,
            this temporary directory is removed.
        /username1
            All experiment sessions committed by the user 'username1' will be kept in this directory. All required data
            for a given session will be stored in two files: {subj_name}_{session_date}_{sfx}.zip is the ZIP file
            uploaded by the user during the original commit, and {subj_name}_{session_date}_{sfx}.pickle is a Python
            pickle file containing data prepared during pre-processing of the archive or entered manually by the user
            during the commit process.
        /username2
            Similarly for all sessions committed by 'username2'

A dynamic web application will serve as the primary interface to the DJ-administered lab database and its associated
raw data repository. This web application will allow an authorized user to perform a variety of tasks:
    1. Add new entities to the various "metadata" tables in the schema -- User, Subject, and so on. All of the changes
    in these relatively small, simple tables are recorded in a  log file in $DATA_ROOT/logs so that the database can be
    reconstructed from scratch.
    2. Upload the raw data files from an experiment session and digest them via helper scripts and the pipeline code.
    This is the most complex and time-consuming task. For each experiment a new Session entity is inserted into the
    database, plus any new TrialProtocols discovered. If neural activity was recorded during the session, an entity
    is added to the Session.Neuron table for each identified neural unit. Then, for each trial recorded during the
    session, an entity is added to the Trial table and its part tables to store the behavioral and neural responses
    during each trial, all aligned on trial start. The Trial table and its parts is by far the largest table in the lab
    database, and it is the only table that is 'auto-populated'.
    3. Explore the lab database, show summary reports for a given experiment session, perform certain analyses on
    selected datasets, export selected datasets for external use.

To simplify initial development, we are making a number of assumptions:
    1) Any electrophysiological recordings are performed with the Omniplex system recording "wide-band" data in a
    single large PL2 file. Later we'll develop methods to extract neural data from the older Plexon MAP system, from
    Plexon MAP and Omniplex "clips", and from neural response data recorded directly in the Maestro trial data files.
    2) The experimenter must supply their "spike sorting" results. This is because every researcher seems to use their
    own spike sorting algorithm, and in some situations "by eye" spike editing happens. Currently, neural unit
    information and spike train data is supplied in a pickle file. See manager.py for details.

Created on Wed Jun  3 14:13:38 2020

@author: sruffner
"""
from __future__ import annotations  # Needed in Python 3.7y to type-hint a method with the type of enclosing class

from typing import Dict, Any, Optional

import datajoint as dj
import time
import warnings

# On first import, we connect to the database and declare the schema (if it is not already defined in database)
while True:
    try:
        db_connection = dj.conn()
        break
    except Exception as connection_error: 
        warnings.warn(RuntimeWarning(
            "Unable to connect to the database with error {0}. Trying again in 5s.".format(connection_error)))
        time.sleep(5)
schema = dj.schema('sgl', connection=db_connection)


@schema
class User(dj.Manual):
    definition = """
    # Members of a laboratory
    username : varchar(20)              # Network login name
    ---
    full_name : varchar(50)             # Full name of lab member. Recommend format as would appear in publication
    contact_email : varchar(80)        # Email address
    role : enum("Principal Investigator", "Post Doctoral Researcher", "Graduate Student", "Administrator")
    """


@schema
class Subject(dj.Manual):
    definition = """
    # Subjects of experimental investigations in laboratory
    subj_id : varchar(20)              # Subject unique nickname
    ---
    species : enum("Macaca mulatta", "Homo sapiens")
    dob : date
    sex : enum('M','F','?')
    """


@schema
class SubjectImplant(dj.Manual):
    definition = """
    # Electrode recording cylinder implantations in an animal subject
    -> Subject
    implant_date : date                 # Date of cylinder implantation
    ---
    st_ap : float                       # stereotaxic location of implant WRT ??, anterior-posterior coordinate  (mm)
    st_ml : float                       # medial-lateral coordinate (mm)
    st_dv : float                       # dorsal-ventral coordinate (mm)
    ap_angle : float                    # cylinder angle relative to AP axis (deg CCW)
    ml_angle : float                    # cylinder angle relative to MP axis (deg CCW)
    """


@schema
class Rig(dj.Manual):
    definition = """
    # Laboratory setups on which experiments are conducted
    rig_id : varchar(10)                # Unique rig nickname
    ---
    rig_loc: varchar(50)                # Identifying location (eg, room number)
    """


@schema
class BrainArea(dj.Manual):
    definition = """
    # Brain regions investigated in laboratory experiments
    ba_id : int auto_increment          # Opaque ID# for brevity (not intended for display)
    ---
    ba_name : varchar(50)               # The name of the brain region. Must be unique.
    unique index (ba_name)
    """


@schema
class NeuronType(dj.Manual):
    definition = """
    # Types of neurons studied in laboratory experiments
    nt_id : int auto_increment          # Opaque ID# for brevity (not intended for display)
    ---
    nt_name : varchar(50)               # The name of the neuron type. Must be unique.
    unique index (nt_name)
    """


@schema
class Study(dj.Manual):
    definition = """
    # Research projects/studies conducted in the laboratory
    study_id: int auto_increment        # Opaque ID# for brevity (not intended for display)
    ---
    study_title : varchar(50)           # Abbreviated project title. Must be unique.
    -> User.proj(study_lead='username') # Lab member with primary responsibility for the study
    study_desc : varchar(2048)          # A fuller description of the project
    unique index (study_title)
    """


@schema
class Publication(dj.Manual):
    definition = """
    # Research publications by members of the laboratory
    pub_id: int auto_increment          # Opaque ID# for brevity (not intended for display)
    ---
    citation : varchar(500)             # Formal citation of research article
    doi : varchar(100)                  # Digital Object Identifier for the publication. Must be unique.
    unique index (doi)
    """


@schema
class StudyPublication(dj.Manual):
    definition = """
    # Publications related to studies conducted in the laboratory
    -> Study
    -> Publication
    """


@schema
class Session(dj.Manual):
    """
    While the Session table is 'manual', new entries are added through an interactive web application. The user must
    compress all session data (Maestro trial files, Omniplex PL2 file(s), and the spike sorting results) into a single
    ZIP archive file which is uploaded through the web app into a staging directory with the data repository. The
    backend server will scan all the Maestro data files within the ZIP (in situ, without extracting the archive) to
    identify the distinct trial protocols presented during the experimental session. It will query the user to verify
    the trial protocols and to collect other session metadata.

    The experimenter MUST provide a file containing the results of their spike-sorting analysis to identify the distinct
    neural units recorded during the session. The exact format of this file is TBD, but it must contain, for each
    identified unit: Omniplex source channel ID, Omniplex source filename (to support multiple Omniplex files recorded
    during one session), and a potentially long vector holding the spike occurrence times (in seconds since the Omniplex
    recording began). The web app will ask the user to specify the (putative) neuron type (choose from an entity in the
    NeuronType table). The backend will analyze the Omniplex raw data to calculate the unit's mean firing rate in Hz,
    signal-to-noise ration, and the average spike waveform template. With the exception of the spike times, this
    information is stored in the Session.Neuron part table.

    Once the script has prepared the session directory and collected all required metadata, it will then insert a new
    entry in the Session table. If the session included an electrophysiological recording, the requisite information is
    inserted as a new entry in the Session.EPhys part table, and any identified neural units are inserted into the
    Session.Neuron part table. It will also insert any new trial protocols into the TrialProtocols table.

    Finally, it will call populate() on the one imported table in this pipeline - Trial.
    """
    definition = """
    # Experimental sessions conducted in the laboratory
    -> User.proj(experimenter='username')  # The user conducting the experiment
    -> Subject                             # The animal subject for the session
    session_date : date                    # Date of session
    session_sfx : tinyint unsigned         # To distinguish multiple sessions on the same date (range [0..9])
    ---
    -> Rig                                 # The lab rig on which experiment session was conducted
    -> Study                               # The research project with which this session is associated
    session_notes : varchar(2048)          # Notes about session
    """

    class EPhys(dj.Part):
        """
         Information on electrophysiological recording that took place during experimental session (if at all). This is
         a part table because not every experiment session includes electrophysiological recordings.
        """
        definition = """
        # Electrophysiological recordings performed during experimental sessions
        -> master
        ---
        ephys_src : enum('Omniplex', 'Omniplex clips', 'Plexon MAP', 'Maestro Waveform', 'Maestro Spike Ch')
        # NOTE: These are the known ways in which neuronal response data have been recorded in the lab. For now, we
        # only support the Omniplex as a source.
        probe_type : enum('single', '32-channel', 'other')
        sampling_rate : float           # Electrode signal sampling rate in Hz
        probe_x : float                 # X,Y location of probe within recording cylinder implant in mm
        probe_y : float
        probe_depth : float             # Insertion depth of probe in mm
        -> BrainArea                    # Desired/target area of brain for the electrode recording
        """

    class Neuron(dj.Part):
        definition = """
        # Distinct neural units culled from extracellular recordings during an experimental session
        -> master
        unit_id : smallint                     # Unique ID assigned to unit
        ---
        unit_channel : varchar(10)             # ID/label for source channel on which unit was recorded
        -> NeuronType.proj(unit_type='nt_id')  # Identified neuron type
        unit_rate : float                      # Mean firing rate of neural unit while held (in Hz)
        unit_spikes : int unsigned             # total number of spikes recorded
        unit_snr : float                       # Signal-to-noise ratio (indication of quality of recording?)
        unit_template : blob                   # Average spike waveform template
        """


@schema
class TrialProtocol(dj.Manual):
    """
    A trial protocol essentially corresponds to a Maestro trial definition and defines the trajectories of visual
    stimuli during the trial. A given trial protocol is typically repeated multiple times over the course of a single
    experiment session and across many different sessions; a trial is just one particular presentation of a trial
    protocol. Typically, every trial rep is unique because it contains at least one random variable -- most notably, the
    random duration of a 'fixation segment' that precedes stimulus onset.

    So, a trial protocol is defined by the fixed part of the trial definition (most of it), plus zero or more random
    variables defining what parameter(s) will vary randomly with each presentation of the protocol. Another tricky
    aspect to this concept is that Maestro lets the experimenter set a 'global target transform', which will transform
    target trajectories without changing the original Maestro trial definition. Since the transform parameters are
    included in each data file along with the trial codes, it is possible to recover the original trial definition.
    However, the transform is used to adapt a trial definition to the spatio-temporal receptive field of a neural unit
    being recorded, so all the reps presented to that unit will use the same transform value. For that reason, the
    global target transform is considered part of the trial protocol, rather than something that changes per trial.

    The purpose of defining a trial protocol is two-fold: (1) To reduce the memory footprint of each individual trial in
    the Trial table -- because we won't have to store the stimulus target trajectories (they can be calculated from the
    trial protocol and some information stored in the individual trial entity. (2) To identify repeated presentations of
    the same trial protocol, for aggregate analyses of behavioral and neuronal responses.

    New trial protocols are inserted into this table when an experiment session is digested by the web app interface
    to the DataJoint pipeline and lab database. All the trial data files are scanned to identify distinct trial
    protocols. Any protocols not already found in the TrialProtocol table will be verified with the user interactively
    through the web app, then inserted into the table.

    A trial protocol's definition is rather complex. Rather than storing it so that any segment or target parameter can
    be accessed via DataJoint queries, the entire definition is stored as a blob in an internal format -- see Protocol
    in maestro.py. Backend server code will load this definition and use it to calculate target trajectories as needed.
    """
    definition = """
    # Stimulus-target trial protocol
    proto_hash : char(32)               # MD5 hash of trial protocol definition (encoded in url-safe Base64 ASCII)
    ---
    proto_name : varchar(50)            # Trial name
    proto_set : varchar(50)             # Name of trial set to which trial belongs (may be empty string)
    proto_subset : varchar(50)          # Name of trial subset to which trial belongs (may be empty string)
    proto_def : blob                    # full trial protocol definition (opaque format: maestro.Protocol)
    """


class TrialProducer:
    """
    The Trial class relies on a trial "producer" to populate the Trial database table and its part tables with all the
    trials for a given experiment session during an auto-populate cycle. This mixin class defines but does not implement
    the single method called during an auto-populate cycle.
    """
    def insert_trials_for_session(self, session_key: Dict[str, Any]) -> None:
        """
        Prepare and insert into the Trial table (and its part tables) all trials presented during the experiment
        session specified. This method is intended to be called only from within Trial.make() during an auto-populate
        cycle (so that Trial.insert() is allowed).

        Args:
            session_key: Primary key identifying an existing session in the lab database.

        Raises:
            Exception: If any error occurs while populating the Trial table and its part tables.
        """
        raise NotImplemented("Derived class must handle the task of inserting trials during auto-populate cycle")


@schema
class Trial(dj.Imported):
    definition = """
    -> Session                             # The experimental session during which the trial was presented
    trial_idx : int unsigned               # Indicates order of presentation during session (starts at 1)
    ---
    -> TrialProtocol                       # trial protocol (aka, Maestro trial definition)
    trial_header : blob                    # Original data file header (in opaque format for use by backend server)
    trial_filename : varchar(50)           # Maestro data filename (ends in 4-digit extension like .0001)
    trial_dur : int unsigned               # recorded duration of trial in milliseconds
    trial_record_start : int unsigned      # if non-zero, recording began this many milliseconds after trial start
    trial_success : boolean                # trial completed successfully
    trial_rewarded : boolean               # could be false if reward earned but was randomly withheld
    trial_rew1: smallint unsigned          # length of reward pulse 1 in milliseconds
    trial_rew2: smallint unsigned          # length of reward pulse 2 in milliseconds
    vstab_win_len: tinyint unsigned        # length of sliding window for smoothing eye pos in VStab (ms, 1..20)
    trial_ts: float                        # trial start timestamp, in elapsed secs since start of first trial in
                                           # session (-1 if not available)
    trial_rvs: blob                        # trial random variable values (opaque format; list of int/float values, in
                                           # same order as maestro.Protocol.diffs; empty list if no protocol RVs)
    """

    _trial_producer: Optional[TrialProducer] = None

    class Event(dj.Part):
        definition = """
        -> master
        event_ch : int unsigned         # Digital input channel number for the TTL event
        ---
        event_times : blob              # Event time(s) in seconds since trial started. 1D Numpy array
        """

    class BehavioralResponse(dj.Part):
        definition = """
        -> master
        response_id : enum('HEPOS', 'VEPOS', 'HEVEL', 'VEVEL', 'HDVEL')
        ---
        response_trace : blob           # Response in deg (or deg/sec) for RECORDED duration of trial. Sample rate =
                                        # 1KHz. 1D Numpy array.
        """

    class NeuronalResponse(dj.Part):
        definition = """
        -> master
        -> Session.Neuron
        ---
        spike_times : blob              # Spike times during trial in seconds since trial start. 1D Numpy array.
        """

    def set_trial_producer(self, producer: Optional[TrialProducer]) -> None:
        self._trial_producer = producer

    def make(self, key):
        """
        This method is called during an auto-populate cycle to populate the Trial table and its part tables with all
        trials presented during a just-added experiment session.

        The implementation delegates the work to a trial "producer" which must be installed via set_trial_producer()
        immediately before invoking Trial.populate() and then removed immediately afterward.

        Args:
            key: This will contain the primary key of the new session.
        """
        if not self._trial_producer:
            raise Exception("Missing trial producer delegate")
        self._trial_producer.insert_trials_for_session(key)
