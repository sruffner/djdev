"""
trial_data_ops.py: Operations that retrieve and collect trial response data from tables in the Lisberger lab portal.

@author: sruffner
@created: 11oct2021
"""
from __future__ import annotations  # Needed in Python 3.7y to type-hint a method with the type of enclosing class

import functools
import pickle
from dataclasses import dataclass
from datetime import date
from typing import List, Union, Dict, Tuple, Optional
import numpy as np
from numpy.lib import stride_tricks

from config.app_logging import get_application_logger
from sglportalutils import maestro
from database.table_info import AttributeValue, DBTable, primary_key_of
from database.table_ops import fetch_one_row, fetch_rows, fetch_restrict_proj, fetch_any_proj, fetch_attribute_values


def trial_protocols_for_session(session_key: Dict[str, AttributeValue], aggregate: bool = False) \
        -> Optional[Dict[str, str]]:
    """
    Get all trial protocols presented during the specified experiment session.

    Args:
        session_key: At a minimum, this dictionary must uniquely identify an experiment session in the database.
        aggregate: If True, include ONLY those trial protocols for which average response data can be computed. By
            convention, there must be at least 3 reps of the trial protocol, AND the protocol itself either must have NO
            random variables OR a single segment (not necessarily segment 0) of random duration. Default = False.
    Returns:
        A dictionary containing the user-friendly pathname ("set/subset/name" or "set/name") of each trial protocol
            presented during the experiment session, keyed by the protocol's MD5 hash digest. If aggregate is True, any
            protocols for which the average responses CANNOT be computed are excluded; in this case, it is possible that
            the dictionary is empty. The dictionary items are ordered by pathname. Returns None if an error occurs.
    """
    try:
        session_pk = {k: session_key[k] for k in primary_key_of(DBTable.SESSION, False)}
        all_proto_hashes = fetch_attribute_values(DBTable.TRIAL, 'proto_hash', restriction=session_pk)
        if all_proto_hashes is None:
            return None
        unique_proto_hashes = {h for h in all_proto_hashes}
        if aggregate:
            out = dict()
            for h in unique_proto_hashes:
                if all_proto_hashes.count(h) > 2:
                    proto = get_trial_protocol_definition(h)
                    if proto is None:
                        return None
                    elif proto.can_aggregate_responses():
                        out[h] = proto.trial.path_name()
        else:
            restriction = [f"proto_hash = '{h}'" for h in unique_proto_hashes]
            protocols = fetch_any_proj(DBTable.TRIAL_PROTOCOL, restriction, ['proto_name', 'proto_set', 'proto_subset'])
            if protocols is None:
                return None
            out = dict()
            for p in protocols:
                out[p['proto_hash']] = f"{p['proto_set']}/{p['proto_name']}" \
                    if len(p['proto_subset']) == 0 else f"{p['proto_set']}/{p['proto_subset']}/{p['proto_name']}"
        sorted_tuples = sorted(out.items(), key=lambda item: item[1])
        return {k: v for k, v in sorted_tuples}
    except Exception as e:
        get_application_logger().error(str(e), exc_info=True)
        return None


def trial_protocols_for_neuron(neuron_key: Dict[str, AttributeValue], aggregate: bool = False) \
        -> Optional[Dict[str, str]]:
    """
    Get all trial protocols presented to the specified neural unit.

    Args:
        neuron_key: At a minimum, this dictionary must uniquely identify a recorded neural unit in the database.
        aggregate: If True, include ONLY those trial protocols for which the average neural response can be
            computed. By convention, there must be at least 3 reps of the trial protocol for which the neural
            response was recorded, AND the protocol itself either must have NO random variables OR a single segment
            (not necessarily segment 0) of random duration. Default = False.
    Returns:
        A dictionary containing the user-friendly pathname ("set/subset/name" or "set/name") of each trial protocol
            presented while recording the response of the neural unit, keyed by the protocol's MD5 hash digest. If
            aggregate is True, any protocols for which the average neural response CANNOT be computed are excluded; in
            this case, it is possible that the dictionary is empty. The dictionary items are ordered by pathname.
            Returns None if an error occurs.
    """
    try:
        neuron_pk = {k: neuron_key[k] for k in primary_key_of(DBTable.SESSION_NEURON, False)}
        res = fetch_restrict_proj([DBTable.TRIAL, DBTable.TRIAL_NEURONAL], [None, neuron_pk], ['proto_hash'])
        if res is None:
            return None
        proto_hashes = {r['proto_hash'] for r in res}
        if aggregate:
            all_reps = [r['proto_hash'] for r in res]
            out = dict()
            for h in proto_hashes:
                if all_reps.count(h) > 2:
                    proto = get_trial_protocol_definition(h)
                    if proto is None:
                        return None
                    elif proto.can_aggregate_responses():
                        out[h] = proto.trial.path_name()
        else:
            restriction = [f"proto_hash = '{h}'" for h in proto_hashes]
            protocols = fetch_any_proj(DBTable.TRIAL_PROTOCOL, restriction, ['proto_name', 'proto_set', 'proto_subset'])
            if protocols is None:
                return None
            out = dict()
            for p in protocols:
                out[p['proto_hash']] = f"{p['proto_set']}/{p['proto_name']}" \
                    if len(p['proto_subset']) == 0 else f"{p['proto_set']}/{p['proto_subset']}/{p['proto_name']}"
        sorted_tuples = sorted(out.items(), key=lambda item: item[1])
        return {k: v for k, v in sorted_tuples}
    except Exception as e:
        get_application_logger().error(str(e), exc_info=True)
        return None


def get_trial_protocol_definition(proto_hash: str) -> Optional[maestro.Protocol]:
    """
    Retrieve the definition of the specified Maestro trial protocol definition from the lab database.

    Args:
        proto_hash: The MD5 hash digest that uniquely identifies the requested trial protocol.
    Returns:
        The trial protocol definition. Returns None if an error occurs or the specified protocol not found.
    """
    try:
        protocol_entry = fetch_one_row(DBTable.TRIAL_PROTOCOL, dict(proto_hash=proto_hash))
        if protocol_entry:
            return pickle.loads(protocol_entry['proto_def'])
    except Exception as e:
        get_application_logger().error(str(e), exc_info=True)
    return None


def trials_for_session(session_key: Dict[str, AttributeValue], proto_hash: Optional[str] = None,
                       complete_reps_only: bool = False) -> Optional[List[int]]:
    """
    Get the indices of all trials, or a subset thereof, presented during the specified experiment session.

    Args:
        session_key: At a minimum, this dictionary must uniquely identify an experiment session in the database.
        proto_hash: If this identifies a trial protocol in the database, then return only the indices of the trials
            belonging to that protocol. Default = None.
        complete_reps_only: If True, omit from result any trials that did not run to completion. Default = False.
    Returns:
        The list of indices of the relevant trials. The trial index indicates its presentation order during the
            experiment session. The index plus the session key is sufficient information to retrieve the trial details
            and response data. Returns an empty list if no relevant trials found. Returns None if an error occurs while
            retrieving the information.
    """
    try:
        restriction = {k: session_key[k] for k in primary_key_of(DBTable.SESSION, False)}
        if isinstance(proto_hash, str):
            restriction['proto_hash'] = proto_hash
        if complete_reps_only:
            restriction['trial_success'] = True
        relevant_trial_indices = fetch_attribute_values(DBTable.TRIAL, 'trial_idx', restriction)
        relevant_trial_indices.sort()
        return relevant_trial_indices
    except Exception as e:
        get_application_logger().error(str(e), exc_info=True)
        return None


def trials_for_neuron(neuron_key: Dict[str, AttributeValue], proto_hash: Optional[str] = None,
                      complete_reps_only: bool = False) -> Optional[List[int]]:
    """
    Get the indices of all trials, or a subset thereof, presented to the specified neural unit.

    Args:
        neuron_key: At a minimum, this dictionary must uniquely identify a recorded neural unit in the database.
        proto_hash: If this identifies a trial protocol in the database, then return only the indices of the trials
            belonging to that protocol. Default = None.
        complete_reps_only: If True, omit from result any trials that did not run to completion. Default = False.
    Returns:
        The list of indices of the relevant trials. The trial index indicates its presentation order during the
            experiment session. The index plus the neural unit's session key is sufficient information to retrieve
            the trial details and response data. Returns an empty list if no relevant trials found. Returns None if
            an error occurs while retrieving the information.
    """
    try:
        neuron_pk = {k: neuron_key[k] for k in primary_key_of(DBTable.SESSION_NEURON, False)}
        trial_restriction = dict()
        if isinstance(proto_hash, str):
            trial_restriction['proto_hash'] = proto_hash
        if complete_reps_only:
            trial_restriction['trial_success'] = True
        relevant_trials = fetch_restrict_proj([DBTable.TRIAL, DBTable.TRIAL_NEURONAL],
                                              [None if len(trial_restriction) == 0 else trial_restriction, neuron_pk])
        if relevant_trials is None:
            return None
        return [t['trial_idx'] for t in relevant_trials]
    except Exception as e:
        get_application_logger().error(str(e), exc_info=True)
        return None


def data_for_trial(trial_key: Dict[str, AttributeValue], unit_ids: Optional[List[int]] = None) -> Optional[TrialData]:
    """
    Retrieve data recorded for a specified trial in the Lisberger lab database.

    Args:
        trial_key: This dictionary must uniquely identify a single trial record in the database.
        unit_ids: Use this argument to request the responses of one or more neural units (identified by their
            integer unit ID). If None, no neuronal responses are retrieved. Default = None.
    Returns:
        The trial data container. Returns None if trial not found or if an error occurs while retrieving the
            information.
    """
    try:
        trial_pk = {k: trial_key[k] for k in primary_key_of(DBTable.TRIAL)}
        trial_info = fetch_one_row(DBTable.TRIAL, trial_pk)
        if trial_info is None:
            get_application_logger().error(f"Trial ({trial_pk}) not found in database!")
            return None
        proto_info = fetch_one_row(DBTable.TRIAL_PROTOCOL, dict(proto_hash=trial_info['proto_hash']))
        if proto_info is None:
            get_application_logger().error(f"Trial protocol (hash={trial_info['proto_hash']}) not found in database!")
            return None
        behavioral_responses = fetch_rows(DBTable.TRIAL_BEHAVIORAL, trial_pk)
        if behavioral_responses is None:
            return None
        neuronal_responses = list() if unit_ids is None else fetch_rows(DBTable.TRIAL_NEURONAL, trial_pk)
        if neuronal_responses is None:
            return None

        behavioral_field: Dict[str, np.ndarray] = dict()
        for response in behavioral_responses:
            behavioral_field[response['response_id']] = response['response_trace']
        neuronal_field: Dict[int, np.ndarray] = dict()
        for response in neuronal_responses:
            if response['unit_id'] in unit_ids:
                neuronal_field[response['unit_id']] = response['spike_times']
        proto_def: maestro.Protocol = pickle.loads(proto_info['proto_def'])

        trial_data: TrialData = TrialData(
            experimenter=trial_pk['experimenter'],
            subj_id=trial_pk['subj_id'],
            session_date=trial_pk['session_date'],
            session_sfx=trial_pk['session_sfx'],
            trial_idx=trial_pk['trial_idx'],
            protocol=proto_def,
            filename=trial_info['trial_filename'],
            duration_ms=trial_info['trial_dur'],
            record_start_ms=trial_info['trial_record_start'],
            success=trial_info['trial_success'],
            rewarded=trial_info['trial_rewarded'],
            reward1_ms=trial_info['trial_rew1'],
            reward2_ms=trial_info['trial_rew2'],
            vstab_win_len_ms=trial_info['vstab_win_len'],
            timestamp_sec=trial_info['trial_ts'],
            trial_rvs=pickle.loads(trial_info['trial_rvs']),
            behavior=behavioral_field,
            neuronal=neuronal_field
        )
        return trial_data
    except Exception as e:
        get_application_logger().error(str(e), exc_info=True)
        return None


def retrieve_trial_block(
        session_key: Dict[str, AttributeValue], start: int, end: int = -1, unit_ids: List[int] = None,
        completed_only: bool = False, remove_saccades: bool = False, include_fixtgts: bool = False
) -> Optional[List[TrialData]]:
    """
    Retrieve data for a specified block of trials recorded during the specified experiment session. NOTE that this
    method could take a significant amount of time depending on the number of trials that must be retrieved from the
    database.

    Args:
        session_key: At a minimum, this dictionary must contain the primary keys that uniquely identify one experiment
            session in the database.
        start: Index of first trial in block. Must lie in [1..N], where N is the number of trials in the session.
        end: Index of last trial in block, where -1 extends block to the last trial. Default = -1.
        unit_ids: Use this argument to request the responses of one or more neural units (identified by their
            integer unit ID). Any invalid unit IDs are ignored. If None or [], no neuronal responses are retrieved.
            Default = None.
        completed_only: If True, skip any trials that did not run to completion. Default = False.
        remove_saccades: If True, eye velocity traces are adjusted for baseline offset and any detecte saccade epochs
            in the traces are replaced with NaNs. Otherwise, the eye velocity traces are as recorded. Default = False.
        include_fixtgts: If True, compute position trajectories of fixation targets and include with retrieved trial
            response data.
    Returns:
        A list of trial data containers. The list could be empty if completed_only=True and there were no completed
            trials in the specified block. Returns None if an error occurs while retrieving the trial data.
    Raises:
        ValueError: If the session was not found or the trial block range is invalid.
    """
    try:
        session_info = fetch_one_row(DBTable.SESSION, session_key)
        if session_info is None:
            raise ValueError("Session not found, or internal database error")
        if (start < 1) or (start > end) or (start > session_info['num_trials']):
            raise ValueError(f"Invalid trial block range {start} - {end}")
        if (end <= 0) or (end > session_info['num_trials']):
            end = session_info['num_trials']
        trial_restrictions = [
            f'experimenter = "{session_key["experimenter"]}"',
            f'subj_id = "{session_key["subj_id"]}"',
            f'session_date = "{str(session_key["session_date"])}"',
            f'session_sfx = {session_key["session_sfx"]}',
            f'trial_idx >= {start}', f'trial_idx <= {end}'
        ]
        if completed_only:
            trial_restrictions.append(f'trial_success = True')

        relevant_trials = fetch_restrict_proj([DBTable.TRIAL], [trial_restrictions], [])
        if relevant_trials is None:
            return None
        relevant_trials.sort(key=lambda k: k['trial_idx'], reverse=True)
        behavioral_responses = \
            fetch_restrict_proj([DBTable.TRIAL_BEHAVIORAL, DBTable.TRIAL], [None, trial_restrictions], [])
        if behavioral_responses is None:
            return None
        behavioral_responses.sort(key=lambda k: k['trial_idx'], reverse=True)
        units: Dict[int, List[Dict[str, AttributeValue]]] = dict()
        if isinstance(unit_ids, list) and (len(unit_ids) > 0):
            neuron_pk = {k: session_key[k] for k in primary_key_of(DBTable.SESSION)}
            for unit_id in unit_ids:
                neuron_pk['unit_id'] = unit_id
                res = fetch_restrict_proj([DBTable.TRIAL_NEURONAL, DBTable.TRIAL], [neuron_pk, trial_restrictions], [])
                if res is None:
                    return None
                res.sort(key=lambda k: k['trial_idx'], reverse=True)
                units[unit_id] = res

        protocols: Dict[str, maestro.Protocol] = dict()
        trial_data: List[TrialData] = list()
        while len(relevant_trials) > 0:
            trial_info = relevant_trials.pop()
            if not (trial_info['proto_hash'] in protocols):
                proto = fetch_one_row(DBTable.TRIAL_PROTOCOL, dict(proto_hash=trial_info['proto_hash']))
                if proto is None:
                    return None
                protocols[trial_info['proto_hash']] = pickle.loads(proto['proto_def'])

            neuronal_field: Dict[int, Optional[np.ndarray]] = dict()
            for unit_id in unit_ids:
                u = units[unit_id]
                # IMPORTANT: A given unit may not have been recorded during a given trial.
                if (len(u) > 0) and u[-1]['trial_idx'] == trial_info['trial_idx']:
                    neuronal_response = u.pop()
                    neuronal_field[unit_id] = neuronal_response['spike_times']
                else:
                    neuronal_field[unit_id] = None

            behavioral_field: Dict[str, np.ndarray] = dict()
            while (len(behavioral_responses) > 0) and \
                    (behavioral_responses[-1]['trial_idx'] == trial_info['trial_idx']):
                response = behavioral_responses.pop()
                behavioral_field[response['response_id']] = response['response_trace']

            trial_rvs = pickle.loads(trial_info['trial_rvs'])
            fix1, fix2 = None, None
            if include_fixtgts:
                fix1, fix2 = protocols[trial_info['proto_hash']].compute_fixation_target_trajectories(
                    trial_rvs=trial_rvs,
                    hgpos=behavioral_field['HEPOS'] if 'HEPOS' in behavioral_field else None,
                    vepos=behavioral_field['VEPOS'] if 'VEPOS' in behavioral_field else None,
                    vstab_win_len=trial_info['vstab_win_len']
                )

            td = TrialData(
                experimenter=trial_info['experimenter'],
                subj_id=trial_info['subj_id'],
                session_date=trial_info['session_date'],
                session_sfx=trial_info['session_sfx'],
                trial_idx=trial_info['trial_idx'],
                protocol=protocols[trial_info['proto_hash']],
                filename=trial_info['trial_filename'],
                duration_ms=trial_info['trial_dur'],
                record_start_ms=trial_info['trial_record_start'],
                success=trial_info['trial_success'],
                rewarded=trial_info['trial_rewarded'],
                reward1_ms=trial_info['trial_rew1'],
                reward2_ms=trial_info['trial_rew2'],
                vstab_win_len_ms=trial_info['vstab_win_len'],
                timestamp_sec=trial_info['trial_ts'],
                trial_rvs=trial_rvs,
                behavior=behavioral_field,
                neuronal=neuronal_field,
                fix1_pos=fix1,
                fix2_pos=fix2
            )
            if remove_saccades:
                hv, vv = td.eye_velocity_saccades_removed()
                if 'HEVEL' in td.behavior:
                    td.behavior['HEVEL'] = hv
                if 'VEVEL' in td.behavior:
                    td.behavior['VEVEL'] = vv
            trial_data.append(td)

        return trial_data
    except KeyError:
        raise ValueError("Incomplete session key")
    except Exception as e:
        get_application_logger().error(str(e), exc_info=True)
        return None


def retrieve_trial_reps_for_neuron(neuron_key: Dict[str, AttributeValue], proto_hash: str,
                                   complete_reps: bool = True) -> Optional[List[TrialData]]:
    """
    Retrieve data for all reps of the specified trial protocol during which the specified neural unit was recorded.
    NOTE that this method could take a significant amount of time depending on the number of trial reps that must be
    retrieved from the database.

    Args:
        neuron_key: At a minimum, this dictionary must uniquely identify a recorded neural unit in the database.
        proto_hash: The MD5 hash uniquely identifying a trial protocol in the database.
        complete_reps: If True, omit from result any trials that did not run to completion. Default = True.
    Returns:
        A list of trial data containers. The list will be empty if no reps were found. Returns None if an error
            occurs while retrieving the information.
    """
    try:
        neuron_pk = {k: neuron_key[k] for k in primary_key_of(DBTable.SESSION_NEURON, False)}
        trial_restriction = {k: neuron_key[k] for k in primary_key_of(DBTable.SESSION)}
        trial_restriction['proto_hash'] = proto_hash
        if complete_reps:
            trial_restriction['trial_success'] = True

        proto_info = fetch_one_row(DBTable.TRIAL_PROTOCOL, dict(proto_hash=proto_hash))
        if proto_info is None:
            get_application_logger().error(f"Trial protocol (hash={proto_hash}) not found in database!")
            return None
        proto_def: maestro.Protocol = pickle.loads(proto_info['proto_def'])

        # retrieve all relevant trials, and the neuronal and behavioral responses for those trials
        relevant_trials = fetch_restrict_proj([DBTable.TRIAL, DBTable.TRIAL_NEURONAL],
                                              [trial_restriction, neuron_pk], [])
        if relevant_trials is None:
            return None
        neuronal_responses = fetch_restrict_proj([DBTable.TRIAL_NEURONAL, DBTable.TRIAL],
                                                 [neuron_pk, trial_restriction], [])
        if neuronal_responses is None:
            return None
        behavioral_responses = fetch_restrict_proj([DBTable.TRIAL_BEHAVIORAL, DBTable.TRIAL, DBTable.TRIAL_NEURONAL],
                                                   [None, trial_restriction, neuron_pk], [])
        if behavioral_responses is None:
            return None

        relevant_trials = sorted(relevant_trials, key=lambda k: k['trial_idx'], reverse=True)
        neuronal_responses = sorted(neuronal_responses, key=lambda k: k['trial_idx'], reverse=True)
        behavioral_responses = sorted(behavioral_responses, key=lambda k: k['trial_idx'], reverse=True)
        trial_data: List[TrialData] = list()
        while len(relevant_trials) > 0:
            trial_info = relevant_trials.pop()
            neuronal_response = neuronal_responses.pop()
            neuronal_field = {neuronal_response['unit_id']: neuronal_response['spike_times']}
            behavioral_field: Dict[str, np.ndarray] = dict()
            while (len(behavioral_responses) > 0) and \
                    (behavioral_responses[-1]['trial_idx'] == trial_info['trial_idx']):
                response = behavioral_responses.pop()
                behavioral_field[response['response_id']] = response['response_trace']

            trial_data.append(TrialData(
                experimenter=neuron_pk['experimenter'],
                subj_id=neuron_pk['subj_id'],
                session_date=neuron_pk['session_date'],
                session_sfx=neuron_pk['session_sfx'],
                trial_idx=trial_info['trial_idx'],
                protocol=proto_def,
                filename=trial_info['trial_filename'],
                duration_ms=trial_info['trial_dur'],
                record_start_ms=trial_info['trial_record_start'],
                success=trial_info['trial_success'],
                rewarded=trial_info['trial_rewarded'],
                reward1_ms=trial_info['trial_rew1'],
                reward2_ms=trial_info['trial_rew2'],
                vstab_win_len_ms=trial_info['vstab_win_len'],
                timestamp_sec=trial_info['trial_ts'],
                trial_rvs=pickle.loads(trial_info['trial_rvs']),
                behavior=behavioral_field,
                neuronal=neuronal_field
            ))

        return trial_data
    except Exception as e:
        get_application_logger().error(str(e), exc_info=True)
        return None


def retrieve_trial_reps_for_session(session_key: Dict[str, AttributeValue], proto_hash: str,
                                    complete_reps: bool = True) -> Optional[List[TrialData]]:
    """
    Retrieve behavioral trial response data for all reps of the specified trial protocol presented during the specified
    experiment session. NOTE that this method could take a significant amount of time depending on the number of trial
    reps that must be retrieved from the database.

    Args:
        session_key: At a minimum, this dictionary must uniquely identify an experiment session in the database.
        proto_hash: The MD5 hash uniquely identifying a trial protocol in the database.
        complete_reps: If True, omit from result any trials that did not run to completion. Default = True.
    Returns:
        A list of trial data containers. The list will be empty if no reps were found. Returns None if an error
            occurs while retrieving the information.
    """
    try:
        trial_restriction = {k: session_key[k] for k in primary_key_of(DBTable.SESSION, False)}
        trial_restriction['proto_hash'] = proto_hash
        if complete_reps:
            trial_restriction['trial_success'] = True

        proto_info = fetch_one_row(DBTable.TRIAL_PROTOCOL, dict(proto_hash=proto_hash))
        if proto_info is None:
            get_application_logger().error(f"Trial protocol (hash={proto_hash}) not found in database!")
            return None
        proto_def: maestro.Protocol = pickle.loads(proto_info['proto_def'])

        # retrieve all relevant trials, and the behavioral responses for those trials
        relevant_trials = fetch_rows(DBTable.TRIAL, trial_restriction)
        if relevant_trials is None:
            return None
        behavioral_responses = fetch_restrict_proj([DBTable.TRIAL_BEHAVIORAL, DBTable.TRIAL],
                                                   [None, trial_restriction], [])
        if behavioral_responses is None:
            return None

        relevant_trials = sorted(relevant_trials, key=lambda k: k['trial_idx'], reverse=True)
        behavioral_responses = sorted(behavioral_responses, key=lambda k: k['trial_idx'], reverse=True)
        trial_data: List[TrialData] = list()
        while len(relevant_trials) > 0:
            trial_info = relevant_trials.pop()
            behavioral_field: Dict[str, np.ndarray] = dict()
            while (len(behavioral_responses) > 0) and \
                    (behavioral_responses[-1]['trial_idx'] == trial_info['trial_idx']):
                response = behavioral_responses.pop()
                behavioral_field[response['response_id']] = response['response_trace']

            trial_data.append(TrialData(
                experimenter=session_key['experimenter'],
                subj_id=session_key['subj_id'],
                session_date=session_key['session_date'],
                session_sfx=session_key['session_sfx'],
                trial_idx=trial_info['trial_idx'],
                protocol=proto_def,
                filename=trial_info['trial_filename'],
                duration_ms=trial_info['trial_dur'],
                record_start_ms=trial_info['trial_record_start'],
                success=trial_info['trial_success'],
                rewarded=trial_info['trial_rewarded'],
                reward1_ms=trial_info['trial_rew1'],
                reward2_ms=trial_info['trial_rew2'],
                vstab_win_len_ms=trial_info['vstab_win_len'],
                timestamp_sec=trial_info['trial_ts'],
                trial_rvs=pickle.loads(trial_info['trial_rvs']),
                behavior=behavioral_field,
                neuronal=dict()
            ))

        return trial_data
    except Exception as e:
        get_application_logger().error(str(e), exc_info=True)
        return None


@dataclass
class TrialData:
    """
    Data container for the results from a single Maestro trial as retrieved from the Lisberger lab database.
    """
    experimenter: str
    """ Username of lab member performing the experiment in which this trial was recorded. """
    subj_id: str
    """ ID of experiment subject. """
    session_date: date
    """ Date of experiment session during which trial was recorded. """
    session_sfx: int
    """ Session suffix (to distinguish multiple sessions on the same date). """
    trial_idx: int
    """ Trial index -- indicates order of presentation during the experiment session. """
    protocol: maestro.Protocol
    """ The trial protocol presented. """
    filename: str
    """ Original filename of the Maestro data file in which behavioral and other data was recorded. """
    duration_ms: int
    """ Recorded duration of this trial, in milliseconds. """
    record_start_ms: int
    """ Time at which recording began after trial start, in milliseconds (typically 0). """
    success: bool
    """ True if trial was completed successfully. """
    rewarded: bool
    """ True if subject was rewarded (could be false if random reward withholding in effect). """
    reward1_ms: int
    """ Duration of reward pulse #1 in milliseconds. """
    reward2_ms: int
    """ Duration of reward pulse #2 in milliseconds. """
    vstab_win_len_ms: int
    """ Window length for smoothing eye position during velocity stabilization, in milliseconds [1..20]."""
    timestamp_sec: float
    """ Trial start timestamp, in seconds since start of first trial in experiment session (<0 if unknown). """
    trial_rvs: List[Union[int, float]]
    """ List of random variable values, in same order in which random variables are defined in trial protocol. """
    behavior: Dict[str, np.ndarray]
    """ Behavioral responses (in deg or deg/sec) for recorded duration of trial, keyed by channel ID. 1KHz rate. """
    neuronal: Dict[int, Optional[np.ndarray]]
    """ 
    Neural unit spike trains during trial - spike times in seconds since trial start. Keyed by unit ID. If a unit
    was recorded during trial but no spikes occurred, the spike train is an empty array. However, if the unit was not
    recorded during the trial, the spike train is None.
    """
    fix1_pos: Optional[np.ndarray] = None
    """ 
    An Tx2 Numpy float array holding the computed position trajectory (H,V) of fixation target #1 over the recorded
    duration T of trial, in degrees and sampled at same rate as behavioral traces. During any epoch in which there is
    no defined fixation target #1, the samples are NaN. If None, either the trajectory has not been computed or the
    trial protocol did not define a fixation target #1. 
    """
    fix2_pos: Optional[np.ndarray] = None
    """ 
    An Tx2 Numpy float array holding the computed position trajectory (H,V) of fixation target #2 over the recorded
    duration T of trial, in degrees and sampled at same rate as behavioral traces. During any epoch in which there is
    no defined fixation target #2, the samples are NaN. If None, either the trajectory has not been computed or the
    trial protocol did not define a fixation target #2. 
    """

    def instantaneous_firing_rate(self, unit_id: int, smooth: bool = False) -> np.ndarray:
        """
        Compute the instantaneous firing rate for a specified neural unit over the course of the trial timeline,
        optionally smoothed with a Gaussian kernel.

        Firing rate R is computed as the reciprocal of inter-spike interval following Lisberger & Pavelko (1986). Let
        the spike times during the trial be [T(1) .. T(N)]. For each t (delta = 1ms) in the interval [T(i)..T(i+1)],
        R(t) = 1/(T(i) - T(i-1)) if t - T(i) < T(i) - T(i-1); else R(t) = 1/(T(i+1) - T(i)). For t < T(1), R(t) = 0.
        For t in [T(N), T(N) + T(N) - T(N-1)], R = 1/(T(N) - T(N-1)). For t > 2*T(N) - T(N-1), R = 0.

        The firing rate trace is optionally smoothed by convolving it with a Gaussian kernel with a width of 2.5ms.

        Args:
            unit_id: Neural unit ID
            smooth: If True, the instantaneous firing rate is smoothed (default = False).
        Returns:
            Instantaneous firing rate per millisecond during trial, in Hz.
        Raises:
            KeyError: If the unit ID is invalid.
        """
        # spike times in seconds, and converted to integer milliseconds (trial timeline DT is 1ms)
        spike_times = self.neuronal[unit_id]
        spikes_ms = np.floor(spike_times*1000.0).astype(int)
        num_spikes = len(spike_times)
        firing_rate = np.zeros(self.duration_ms)
        if num_spikes < 2:
            return firing_rate   # not enough spikes to compute firing rate

        for i in range(num_spikes):
            t = spikes_ms[i]
            if i == 0:
                t_plus = spikes_ms[i+1]
                firing_rate[t:t_plus] = 1.0 / (spike_times[i+1] - spike_times[i])
            elif i == num_spikes - 1:
                t_minus = spikes_ms[i-1]
                t_last = min(2*t - t_minus, self.duration_ms - 1)
                firing_rate[t:t_last+1] = 1.0 / (spike_times[i] - spike_times[i-1])
            else:
                t_minus = spikes_ms[i-1]
                t_plus = spikes_ms[i+1]
                firing_rate[t:t_plus] = 1.0 / (spike_times[i] - spike_times[i-1])
                if 2*t - t_minus < t_plus:
                    firing_rate[2*t - t_minus:t_plus] = 1.0 / (spike_times[i+1] - spike_times[i])

        if smooth:
            width = 2.5  # in milliseconds  -- could make this a parameter to method
            x = np.arange(-10 * width, 10 * width)
            kernel = np.exp(-x**2/2.0) / (width * np.sqrt(2*np.pi))
            kernel = kernel / sum(kernel)
            firing_rate = np.convolve(firing_rate, kernel, mode='same')

        return firing_rate

    def eye_velocity_saccades_removed(
            self, offset: bool = True, t_vel: float = 20, t_vel_max: float = 50, t_acc: float = 1250,
            t_acc_max: float = 2000, pre_ticks: int = 2, post_ticks: int = 5) -> Tuple[np.ndarray, np.ndarray]:
        """
        Return the horizontal and vertical eye velocity traces for this trial with any saccade epochs replaced by NaN
        samples. This method ASSUMES a sampling rate of 1KHz!

        Args:
            offset: If True, the eye velocity traces are adjusted for DC offset, if possible. Default = True.
            t_vel: Velocity threshold for a saccade. Default = 20 deg/sec
            t_vel_max: Max velocity threshold for a saccade regardless the current acceleration. Default = 50 deg/sec.
            t_acc: Acceleration threshold for a saccade. Default = 1250 deg/sec^2
            t_acc_max: Max acceleration threshold for a saccade regardless the current velocity. Default = 2000.
            pre_ticks: Number of samples before a detected saccade epoch that are included in that epoch. Default = 2.
            post_ticks: # of samples after a detected saccade epoch that are included in that epoch. Default = 5.
        Returns:
            A 2-tuple (H, V) -- COPIES of the horizontal and vertical eye velocity traces in which any samples falling
                within a detected saccade epoch are replaced with NaN. If either velocity trace was not recorded, it is
                assumed to be 0 for the entire duration of the trial.
        """
        # handle edge cases: only H, only V, or no eye velocity trace available
        if not (('HEVEL' in self.behavior) and ('VEVEL' in self.behavior)):
            return np.zeros(self.duration_ms, dtype=np.float32), np.zeros(self.duration_ms, dtype=np.float32)
        if 'HEVEL' in self.behavior:
            hevel = np.copy(self.behavior['HEVEL'])
            if offset:
                hevel = hevel - self.estimate_velocity_baseline_offset('HEVEL')
        else:
            hevel = np.zeros(self.duration_ms, dtype=np.float32)
        if 'VEVEL' in self.behavior:
            vevel = np.copy(self.behavior['VEVEL'])
            if offset:
                vevel = vevel - self.estimate_velocity_baseline_offset('VEVEL')
        else:
            vevel = np.zeros(self.duration_ms, dtype=np.float32)

        speed = np.sqrt(hevel ** 2 + vevel ** 2)
        acceleration = np.diff(speed) / 0.001   # sampling rate = 1KHz!!
        acceleration = np.append(acceleration, np.nan)
        acceleration = np.abs(acceleration)

        in_saccade = np.intersect1d(np.where(speed > t_vel)[0], np.where(acceleration > t_acc)[0])
        in_saccade = functools.reduce(
            np.union1d, (in_saccade, np.where(speed > t_vel_max)[0], np.where(acceleration > t_acc_max)[0]))

        saccading = False
        onset_indices = []
        offset_indices = []
        for i in range(1, len(in_saccade)):
            if in_saccade[i] == in_saccade[i-1] + 1:
                if not saccading:
                    onset_indices.append(max(0, in_saccade[i-1]-pre_ticks))
                    saccading = True
            elif saccading and (in_saccade[i] >= in_saccade[i-1]+post_ticks):
                offset_indices.append(in_saccade[i-1] + post_ticks)
                saccading = False
        if saccading:
            offset_indices.append(min(len(speed), in_saccade[-1]+post_ticks))

        for i in range(len(offset_indices)):
            hevel[onset_indices[i]:offset_indices[i]] = np.nan
            vevel[onset_indices[i]:offset_indices[i]] = np.nan

        return hevel, vevel

    def estimate_velocity_baseline_offset(self, response_id: str) -> float:
        """
        Estimate the baseline offset for an eye velocity trace from this trial. This method examines the corresponding
        position traces and looks for a contiguous segment spanning 100 samples (100ms) in which the position varies
        by 0.1 degrees or less AND the velocity varies by 2 deg/s or less -- in which case eye velocity should be close
        to 0 (and not in the tail of a saccade!). If it finds such a segment, the baseline offset in the velocity trace
        is the mean value over the same segment in the original eye velocity trace.

        Args:
            response_id: Must be 'HEVEL', 'VEVEL', or 'HDVEL'.

        Returns:
            Estimated baseline offset in the specified behavioral trace. Returns 0 if the offset cannot be estimated for
                whatever reason (missing velocity or position signal, signal trace is less than 200ms, or cannot find
                a 100-ms contiguous segment meeting requirements stated above).
        """
        if (response_id.find('VEL') == -1) or (not (response_id in self.behavior)) or (len(self.behavior) < 200):
            return 0
        pos_id = 'HEPOS' if response_id.find('H') > -1 else 'VEPOS'
        if not (pos_id in self.behavior):
            return 0

        pos = self.behavior[pos_id]
        pos_chunks_ok = np.where(
            np.apply_along_axis(lambda x: np.nanmax(x)-np.nanmin(x) < 0.1, 1,
                                stride_tricks.sliding_window_view(pos, window_shape=100)))[0]
        if len(pos_chunks_ok) == 0:
            return 0
        vel = self.behavior[response_id]
        vel_chunks_ok = np.where(
            np.apply_along_axis(lambda x: np.nanmax(x)-np.nanmin(x) < 2, 1,
                                stride_tricks.sliding_window_view(vel, window_shape=100)))[0]
        if len(vel_chunks_ok) == 0:
            return 0
        chunks_ok = np.intersect1d(pos_chunks_ok, vel_chunks_ok)
        if len(chunks_ok) == 0:
            return 0
        start = chunks_ok[0]
        # noinspection PyTypeChecker
        return np.nanmean(vel[start:start+100])
