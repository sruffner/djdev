"""
sgl_schema.py: A database schema for the Lisberger laboratory.

08jul2020: Discarding some of the manual tables that are mostly fluff. We can add them back in once we have a working
full-stack app with DataJoint backend and Dash frontend with web server in a Docker compose application. It will be
some time before we get to that point!

This module defines all of the DataJoint classes (aka database tables) comprising a common framework pipeline for the
Lisberger laboratory. The primary purpose of this framework pipeline  is to "digest" raw data from experiment sessions
into a MySQL database, storing the behavioral and neural data, along with stimulus protocols and other important
metadata in a logical structure that will facilitate finding/selecting specific data collections within the database.

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

Here is the prescribed layout for the repository's directory structure:
    $DATA_ROOT
        /metadata  (content for lab-wide lookup and manual tables)
            1-metadata.json
            2-metadata.json
            ...
        /trialprotocols (all distinct trial protocols are defined in JSON files within this directory)
            1-proto.json
            2-proto.json
            ...
        /username1
            /studyName
                /animalName
                    /session_date+suffix (in case of multiple sessions on same date)
                        session-metadata.json (session meta-data required to commit without user interaction)
                        neural-units.mat OR neural-units.npy (experimenter-curated neuron spike train data)
                        /maestro
                            Maestro data files (ending in .0001, ...)
                            ?? Maestro experiment document(s) -- for record-keeping only
                        /omniplex
                            Omniplex (2nd gen Plexon) data files (PL2)
                            -OR-
                            Plexon MAP data files
            ...
        /username2 ...

A dynamic web application will serve as the primary interface to the DJ-administered lab database and its associated
raw data repository. This web application will allow an authorized user to perform a variety of tasks:
    1. Add new entities to the various "metadata" tables in the schema -- Lab, User, Subject, Rig, Study, Publication,
    BrainArea, NeuronType, and so on. All of the information in these relatively small, simple tables are backed up by
    the contents of the JSON files in $DATA_ROOT/metadata.
    2. Upload the raw data files from an experiment session and digest them via helper scripts and the pipeline code.
    This is the most complex and time-consuming task. For each experiment a new Session entity is inserted into the
    database, plus any new TrialProtocols discovered. If neural activity was recorded during the session, an entity
    is added to the Session.Neuron table for each identified neural unit. Then, for each trial recorded during the
    session, an entity is added to the Trial table and its part tables to store the behavioral and neural responses
    during each trial, all aligned on trial start. The Trial table and its parts is by far the largest table in the lab
    database, and it is the only table that is 'auto-populated'.
    3. (LATER) Explore the lab database, show summary reports for a given experiment session, perform certain analyses
    on selected datasets, export selected datasets for external use.

To simplify initial development, we are making a number of assumptions:
    1) Any electrophysiological recordings are performed with the Omniplex system recording "wide-band" data in a
    single large PL2 file. Later we'll develop methods to extract neural data from the older Plexon MAP system, from
    Plexon MAP and Omniplex "clips", and from neural response data recorded directly in the Maestro trial data files.
    2) The experimenter must supply their "spike sorting" results. This is because every researcher seems to use their
    own spike sorting algorithm, and in some situations "by eye" spike editing happens. TODO: We still need to specify
    the exact format for the spike train data. Anticipate that spike occurrence times will be in the Omniplex timeline.
    Need to fully identify the Omniplex channel from which the spike train was extracted.

Created on Wed Jun  3 14:13:38 2020

@author: sruffner
"""

import datajoint as dj

schema = dj.schema('sgl')


@schema
class User(dj.Manual):
    definition = """
    # Members of a laboratory
    username : varchar(20)              # Network login name
    ---
    full_name : varchar(50)             # Full name of lab member. Recommend format as would appear in publication
    contact_email : varchar(100)        # Email address
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
    brain_area : varchar(20)            # Concise abbreviation for brain region (unique)
    ---
    brain_area_desc : varchar(255)      # Longer name for brain region, with description if necessary
    """


@schema
class NeuronType(dj.Manual):
    definition = """
    # Types of neurons studied in laboratory experiments
    neuron_type : varchar(20)           # Concise name or abbreviation for the neuron type (unique)
    ---
    neuron_type_desc : varchar(255)     # Longer description for the neuron type
    """


@schema
class BrainAreaNeuronType(dj.Manual):
    definition = """
    # Neuron types in a particular brain regions (a given neuron type may be found in more than one brain area)
    -> BrainArea
    -> NeuronType
    """


@schema
class Study(dj.Manual):
    definition = """
    # Research projects/studies conducted in the laboratory
    study : varchar(20)                 # Short nickname for the study
    ---
    (study_lead) -> User                # Lab member with primary responsibility for the study
    study_desc : varchar(2048)          # A fuller description of the project
    """


@schema
class Keyword(dj.Manual):
    definition = """
    # Keywords categorizing laboratory research
    keyword : varchar(20)               # The keyword
    """


@schema
class StudyKeyword(dj.Manual):
    definition = """
    # Keywords associated with particular research studies in the laboratory
    -> Study
    -> Keyword
    """


@schema
class Publication(dj.Manual):
    definition = """
    # Research publications by members of the laboratory
    pub_id: varchar(20)                 # A short, abbreviated name for the publication (must be unique)
    ---
    doi  : varchar(100)                 # Digital Object Identifier for the publication
    citation : varchar(255)             # Formal citation of research article
    """


@schema
class StudyPublication(dj.Manual):
    definition = """
    # Publications related to studies conducted in the laboratory
    -> Study
    -> Publication
    """


"""
Data files for each experimental session are digested from a raw data repository with a prescribed directory layout.
Given the experimenter's username, the study name, the subject ID, and the session date and suffix, all data files
(both behavioral trial data and, if relevant, any electrophysiological recordings) will be found at:
    $SESSION_ROOT = $DATA_ROOT/username/study/subj_id/YYYY_MM_DD_sfx/

The session directory is laid out as follows:
    $SESSION_ROOT
        session-metadata.json (session meta-data required to commit without user interaction)
        neural-units.mat OR neural-units.npy (experimenter-curated neuron spike train data)
        /maestro
            Maestro data files (ending in .0001, ...)
            ?? Maestro experiment document(s) -- for record-keeping only
        /omniplex
            Omniplex (2nd gen Plexon) data files (PL2)
            -OR-
            Plexon MAP data files

While the Session table is 'manual', new entries are added through an interactive web application. First, the session
data files (Maestro trial files, Omniplex PL2 file(s), and the spike sorting results) are pushed into the raw data
repository as described above. The app will scan all the Maestro data files to identify the distinct trial protocols
presented during the experimental session. It will query the user to verify the trial protocols and to collect other
session metadata. The general session metadata is saved in session-metadata.json in the above directory, while any new
trial protocols are persisted in individual JSON files in $DATA_ROOT/trialprotocols/. These files are stored in the
data repository so that the lab database can be repopulated without intervention if a catastrophic failure occurs.

The experimenter MUST provide a neural-units.* file containing the results of their spike-sorting analysis to identify
the distinct neural units recorded during the session. The exact format of this file is TBD, but it must contain, for
each identified unit: Omniplex/Plexon source channel number, mean firing rate in Hz, signal-to-noise ratio, a vector
holding the average spike waveform, the sample interval for that waveform, a potentially long vector holding the
spike occurrence times (in seconds) across the session timeline. The interactive script will ask the user to specify
the (putative) neuron type (choose from an entity in the NeuronType table). With the exception of the spike times, this
information is stored in the Session.Neuron part table.

Once the script has prepared the session directory and collected all required metadata, it will then insert a new
entry in the Session table. If the session included an electrophysiological recording, the requisite information is
inserted as a new entry in the Session.EPhys part table, and any identified neural units are inserted into the
Session.Neuron part table. It will also insert any new trial protocols into the TrialProtocols table.

Finally, it will call populate() on the one imported table in this pipeline - Trial.
"""


@schema
class Session(dj.Manual):
    definition = """
    # Experimental sessions conducted in the laboratory
    -> Subject                          # The animal subject for the session
    session_date : date                 # Date of session
    session_sfx : tinyint unsigned      # To distinguish multiple sessions on the same date
    ---
    (experimenter) -> User              # The user conducting the experiment
    -> Rig                              # The lab rig on which experiment session was conducted
    session_notes : varchar(2048)       # Notes about session
    """

    """
    Information on electrophysiological recording that took place during experimental session (if at all). This is a
    part table because not every experiment session includes electrophysiological recordings.
    """
    class EPhys(dj.Part):
        definition = """
        # Electrophysiological recordings performed during experimental sessions
        -> master
        ---
        ephys_src : enum('Omniplex', 'Omniplex clips', 'Plexon MAP', 'Maestro Waveform', 'Maestro Spike Ch')
        # NOTE: These are the known ways in which neuronal response data have been recorded in the lab. For now, we
        # only support the Omniplex as a source, and expect a single Omniplex PL2 file per session.
        probe_type : enum('single', '32-channel', 'other')
        sampling_rate : float           # Electrode signal sampling rate in Hz
        probe_x : float                 # X,Y location of probe within recording cylinder implant (units?)
        probe_y : float
        probe_depth : float             # Insertion depth of probe (units?)
        -> BrainArea                    # Desired/target area of brain for the electrode recording
        """

    class Neuron(dj.Part):
        definition = """
        # Distinct neural units culled from extracellular recordings during an experimental session
        -> master
        unit_id : smallint                  # Unique ID assigned to unit
        ---
        unit_channel : smallint             # Number of source channel on which unit was recorded
        (unit_type) -> NeuronType           # Identified neuron type
        unit_firing_rate : float            # Mean firing rate of neural unit while held (in Hz)
        unit_snr : float                    # Signal-to-noise ratio (indication of quality of recording?)
        unit_template : longblob            # Template waveform used for spike detection ??
        """


"""
A trial protocol essentially corresponds to a Maestro trial definition and defines the trajectories of visual stimuli
during the trial. A given trial protocol is typically repeated multiple times over the course of a single experiment
session; a trial rep is just one particular instance of a trial protocol. Typically, every trial rep is unique because
it contains at least one random variable -- most notably, the random duration of a 'fixation segment' that precedes
stimulus onset.

So, a trial protocol is defined by the fixed part of the trial definition (most of it), plus zero or more random
variables defining what parameter(s) will vary randomly with each presentation of the protocol. Another tricky aspect
to this concept is that Maestro lets the experimenter set a 'global target transform', which will transform target
trajectories without changing the original Maestro trial definition. Since the transform parameters are included in
each data file along with the trial codes, it is possible to recover the original trial definition.

The purpose of defining a trial protocol is two-fold: (1) To reduce the memory footprint of each individual trial in
the Trial table -- because we won't have to store the stimulus target trajectories (they can be calculated from the
trial protocol and some information (like the global transform) stored in the individual trial entity. (2) To identify
repeated presentations of the same trial protocol, for aggregate analyses of behavioral and neuronal responses.

New trial protocols are inserted into this table when an experiment session is digested by the web application
interface to the DataJoint pipeline and lab database. All the trial data files are scanned to identify distinct trial
protocols. Any protocols not already found in the TrialProtocol table will be verified with the user interactively
through the web app, then inserted into the table.

TODO: Add attributes that tie a trial protocol to a behavior like "smooth pursuit"? Other broad characterizations of
stimulus paradigms? The idea here is to be able to search for particular trial protocols in a meaningful way.
"""


@schema
class TrialProtocol(dj.Manual):
    definition = """
    # Stimulus-target trial protocol
    proto_hash : char(24)               # MD5 hash of trial protocol definition (encoded in url-safe Base64 ASCII)
    ---
    proto_name : varchar(50)            # Trial name
    proto_set : varchar(50)             # Name of trial set to which trial belongs
    proto_subset : varchar(50)          # Name of trial subset to which trial belongs (may be empty string)
    proto_segs : tinyint unsigned       # Number of trial segments
    proto_tgts : tinyint unsigned       # Number of participating targets
    proto_perts : tinyint unsigned      # Number of perturbations
    proto_sects : tinyint unsigned      # Number of tagged sections
    proto_json : longblob               # trial protocol definition in JSON format (includes seg table, targets, etc)
    """

    class RandomVariable(dj.Part):
        definition = """
        # Trial segment table parameters that vary randomly with each repeat presentation of trial protocol
        -> master
        var_type : enum('segdur','hpos','vpos','hvel','vvel','hacc','vacc','hpatvel','vpatvel','hpatacc','vpatacc')
        seg_idx : tinyint unsigned
        tgt_idx : tinyint unsigned      # ignored when variable type is segment duration
        """


@schema
class Trial(dj.Imported):
    definition = """
    -> Session                          # The experimental session during which the trial was presented
    trial_idx : int unsigned            # Indicates order of presentation during session (starts at 1)
    ---
    header_json : longblob              # Original data file header in JSON format (to retrieve rarely used params)
    trial_filename : varchar(50)        # Maestro data filename (ends in 4-digit extension like .0001)
    trial_dur : int unsigned            # recorded duration of trial in milliseconds
    trial_record_start : int unsigned   # if non-zero, recording began this many milliseconds after trial start
    disp_w_pix : smallint unsigned      # width of video display in pixels
    disp_h_pix : smallint unsigned      # height of video display in pixels
    disp_w_mm : smallint unsigned       # width of video display in mm
    disp_h_mm : smallint unsigned       # height of video display in mm
    disp_d_mm : smallint unsigned       # perpendicular distance from eye to video display, in mm
    disp_rate_hz : float                # refresh rate of video display in Hz
    xfm_offset_h : float                # global target transform in effect -- horizontal position offset (deg)
    xfm_offset_v : float                # vertical position offset (deg)
    xfm_pos_scale : float               # position scale factor
    xfm_pos_rotate : float              # position rotation angle (deg CCW)
    xfm_vel_scale : float               # velocity scale factor
    xfm_vel_rotate : float              # velocity rotation angle (deg CCW)
    reward1_len : smallint unsigned     # length of reward pulse 1 in msecs
    reward2_len : smallint unsigned     # length of reward pulse 2 in msecs
    success : boolean                   # trial completed successfully
    reward_given : boolean              # could be false if reward earned but was randomly withheld
    timestamp_time : float              # trial start timestamp, in seconds elapsed since a reference time -- either
                                        # the in-file timestamp (since Maestro started) or based on file creation time
                                        # (reported relative to start of first trial in session).
    timestamp_ref : enum('internal', 'filecreate')
    -> TrialProtocol                    # trial protocol details (aka, Maestro trial definition)
    """

    class RandomVarValue(dj.Part):
        definition = """
        # Value of random variables for this instance of trial protocol. For segment duration, value is in msecs. For
        # target trajectory params, divide scaled integer by 1000, then round to 2 fractional digits to recover
        # target position in deg, velocity in deg/sec, or acceleration in deg/sec^2.
        -> master
        -> TrialProtocol.RandomVariable
        rv_value : int                  # The value (scaled by 1000 for target trajectory params)
        """

    class BehavioralResponse(dj.Part):
        definition = """
        -> master
        response_id : enum('HEPOS', 'VEPOS', 'HEVEL', 'VEVEL', 'HDVEL')
        ---
        response_trace : longblob       # The response in deg (or deg/sec), from trial start. If recording started
                                        # AFTER trial start, initial samples are NaN. Sample rate = 1KHz.
        """

    class NeuronalResponse(dj.Part):
        definition = """
        -> master
        -> Session.Neuron
        ---
        spike_times : longblob          # Spike times recorded during trial in seconds since trial start
        """

    def make(self, key):
        pass
