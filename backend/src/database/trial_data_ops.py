"""
trial_data_ops.py: Operations that retrieve and collect trial response data from tables in the Lisberger lab portal.

@author: sruffner
@created: 11oct2021
"""
from __future__ import annotations  # Needed in Python 3.7y to type-hint a method with the type of enclosing class

import json
from typing import List, Union, Dict, Optional, Any

from config.app_logging import get_application_logger
from sglportalapi import maestro
from database.table_info import AttributeValue, DBTable, primary_key_of
from database.table_ops import fetch_one_row, fetch_rows, fetch_restrict_proj, fetch_any_proj, fetch_attribute_values
from sglportalapi.data_containers import TrialRep


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
                    elif proto.can_aggregate_responses:
                        out[h] = proto.trial.path_name
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
                    elif proto.can_aggregate_responses:
                        out[h] = proto.trial.path_name
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
            return maestro.Protocol.from_bytes(protocol_entry['proto_def'])
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


def retrieve_session_trial_rep(
        session_key: Dict[str, Any], trial_index: int, unit_ids: List[int]) -> Union[str, TrialRep]:
    """
    Retrieve metadata and recorded response data for a single specified trial rep in the Lisberger lab database.

    Args:
        session_key: The primary key identifying an experiment session in the database.
        trial_index: Index of the trial rep to be retrieved.
        unit_ids: Use this argument to request the responses (ie, spike trains) of one or more neural units (identified
            by their integer unit ID). If None, no neuronal responses are retrieved. Default = None.
    Returns:
        The trial rep, or an error description if the operation failed for any reason.
    """
    try:
        trial_pk = session_key.copy()
        trial_pk['trial_idx'] = trial_index
        trial_row = fetch_one_row(DBTable.TRIAL, trial_pk)
        if trial_row is None:
            get_application_logger().error(f"Trial ({trial_pk}) not found in database!")
            return "Requested trial rep not found, or database error"
        proto_info = fetch_one_row(DBTable.TRIAL_PROTOCOL, dict(proto_hash=trial_row['proto_hash']))
        if proto_info is None:
            get_application_logger().error(f"Trial protocol (hash={trial_row['proto_hash']}) not found in database!")
            return"Protocol for trial rep not found, or database error"
        behavioral_responses = fetch_rows(DBTable.TRIAL_BEHAVIORAL, trial_pk)
        neuronal_responses = fetch_rows(DBTable.TRIAL_NEURONAL, trial_pk)
        events = fetch_rows(DBTable.TRIAL_EVENT, trial_pk)
        if (behavioral_responses is None) or (neuronal_responses is None) or (events is None):
            get_application_logger().error(f"DB error occurred while retrieving response data for trial {trial_pk}")
            return "An internal database error occured"

        # fix the fields of trial_info to match what is expected for the TrialRep container
        trial_info: Dict[str, Any] = dict()
        for k in trial_row.keys():
            trial_info[k] = trial_row[k]
        trial_info['session_date'] = str(trial_info['session_date'])  # ensure date is as an ISO-formatted string
        trial_info.pop('trial_header', None)  # don't need the trial header object
        trial_info['protocol'] = maestro.Protocol.from_bytes(proto_info['proto_def'])  # want protocol, not its hash
        trial_info.pop('proto_hash', None)
        trial_info['trial_rvs'] = json.loads(trial_info['trial_rvs'].decode())  # deserialize the RV values list
        trial_info['trial_success'] = (trial_info['trial_success'] != 0)   # DJ stores bool as int
        trial_info['trial_rewarded'] = (trial_info['trial_rewarded'] != 0)

        trial_info['hgpos'], trial_info['vepos'], trial_info['hevel'], trial_info['vevel'] = None, None, None, None
        for response in behavioral_responses:
            if response['response_id'] == 'HEPOS':
                trial_info['hgpos'] = response['response_trace']
            elif response['response_id'] == 'VEPOS':
                trial_info['vepos'] = response['response_trace']
            elif response['response_id'] == 'HEVEL':
                trial_info['hevel'] = response['response_trace']
            elif response['response_id'] == 'VEVEL':
                trial_info['vevel'] = response['response_trace']

        trial_info['spike_trains'] = dict()
        if len(unit_ids) > 0:
            # for any unit requested that was not recorded during trial, spike times array must be None!
            for i in unit_ids:
                trial_info['spike_trains'][i] = None
            for response in neuronal_responses:
                if response['unit_id'] in unit_ids:
                    trial_info['spike_trains'][response['unit_id']] = response['spike_times']

        trial_info['events'] = dict()
        for event_row in events:
            trial_info['events'][event_row['event_ch']] = event_row['event_times']

        return TrialRep(trial_info)
    except Exception as e:
        get_application_logger().error(str(e), exc_info=True)
        return f"An internal database error occured [{str(e)}]"


def retrieve_session_trial_reps(
        session_key: Dict[str, Any], proto_hash: Optional[str] = None, start: int = 1, end: int = 1,
        completed: bool = True, unit_ids: Optional[List[int]] = None) -> Union[str, List[TrialRep]]:
    """
    Retrieve metadata and recorded response data for a selection of trial reps recorded during an experiment session
    stored in the portal database -- either a contiguous block of trials OR all trials belonging to the specified trial
    protocol.

    Args:
        session_key: The primary key identifying an experiment session in the database.
        proto_hash: If not None, restrict trial list to all reps of the trial protocol identified by this MD5 hash. In
            this case, the arguments defining a sequential block of trials are ignored. Default = None.
        start: Index of first trial in a sequential trial block. Ignored if 'proto_hash' is specified.
        end: Index of last trial in a sequential trial block. Ignored if 'proto_hash' is specified.
        completed: If true, only successfully completed trial reps are included in the results. Default = True.
        unit_ids: Use this argument to request the responses (ie, spike trains) of one or more neural units (identified
            by their integer unit ID). If None, no neuronal responses are retrieved. Default = None.
    Returns:
        The list of trial reps retrieved, or an error description if the operation failed for any reason.
    """
    try:
        session_info = fetch_one_row(DBTable.SESSION, session_key)
        if session_info is None:
            raise ValueError("Session not found, or internal database error")

        # we're either getting all reps of a single protocol, or all reps in a sequential block
        proto: Optional[maestro.Protocol] = None
        if proto_hash is not None:
            proto_row = fetch_one_row(DBTable.TRIAL_PROTOCOL, dict(proto_hash=proto_hash))
            if proto_row is None:
                return f"Trial protocol (hash={proto_hash}) not found in database!"
            proto = maestro.Protocol.from_bytes(proto_row['proto_def'])
            trial_restrictions = session_key.copy()
            trial_restrictions['proto_hash'] = proto_hash
            if completed:
                trial_restrictions['trial_success'] = True
        else:
            if (start < 1) or (start > session_info['num_trials']) or (start > end):
                return f"Bad trial block range: [{start} .. {end}]"
            trial_restrictions = [
                f'experimenter = "{session_key["experimenter"]}"',
                f'subj_id = "{session_key["subj_id"]}"',
                f'session_date = "{str(session_key["session_date"])}"',
                f'session_sfx = {session_key["session_sfx"]}',
                f'trial_idx >= {start}', f"trial_idx <= {min(end, session_info['num_trials'])}"
            ]

        relevant_trials = fetch_restrict_proj([DBTable.TRIAL], [trial_restrictions], [])
        if relevant_trials is None:
            return "An internal error occurred while retrieving trial reps"
        relevant_trials.sort(key=lambda x: x['trial_idx'], reverse=True)
        behavioral_responses = \
            fetch_restrict_proj([DBTable.TRIAL_BEHAVIORAL, DBTable.TRIAL], [None, trial_restrictions], [])
        if behavioral_responses is None:
            return "An internal error occurred while retrieving behavioral responses for trial reps"
        behavioral_responses.sort(key=lambda x: x['trial_idx'], reverse=True)
        events = \
            fetch_restrict_proj([DBTable.TRIAL_EVENT, DBTable.TRIAL], [None, trial_restrictions], [])
        if events is None:
            return "An internal error occurred while retrieving marker events for trial reps"
        events.sort(key=lambda x: x['trial_idx'], reverse=True)

        units: Dict[int, List[Dict[str, Any]]] = dict()
        if isinstance(unit_ids, list) and (len(unit_ids) > 0):
            neuron_pk = session_key.copy()
            for unit_id in unit_ids:
                neuron_pk['unit_id'] = unit_id
                res = fetch_restrict_proj([DBTable.TRIAL_NEURONAL, DBTable.TRIAL],
                                          [neuron_pk, trial_restrictions], [])
                if res is None:
                    return "An internal error occurred while retrieving neural response data for trial reps"
                res.sort(key=lambda x: x['trial_idx'], reverse=True)
                units[unit_id] = res

        protocols: Dict[str, maestro.Protocol] = dict()
        trial_reps: List[TrialRep] = list()
        while len(relevant_trials) > 0:
            trial_row = relevant_trials.pop()
            trial_info: Dict[str, Any] = dict()
            for k in trial_row.keys():
                trial_info[k] = trial_row[k]
            trial_info['session_date'] = str(trial_info['session_date'])  # want the date as an ISO-formatted string
            trial_info.pop('trial_header', None)  # don't need the trial header object
            trial_info['trial_rvs'] = json.loads(trial_info['trial_rvs'].decode())  # deserialize the RV values list
            trial_info['trial_success'] = (trial_info['trial_success'] != 0)   # DJ stores bool as int
            trial_info['trial_rewarded'] = (trial_info['trial_rewarded'] != 0)

            # when retrieving a sequential block of trials, the protocol will typically be different for each rep
            if proto is None:
                if not (trial_info['proto_hash'] in protocols):
                    proto_row = fetch_one_row(DBTable.TRIAL_PROTOCOL, dict(proto_hash=trial_info['proto_hash']))
                    if proto_row is None:
                        return "An internal error occurred while retrieving a trial protocol object"
                    protocols[trial_info['proto_hash']] = maestro.Protocol.from_bytes(proto_row['proto_def'])
                trial_info['protocol'] = protocols[trial_info['proto_hash']]
            else:
                trial_info['protocol'] = proto
            trial_info.pop('proto_hash', None)

            trial_info['hgpos'], trial_info['vepos'], trial_info['hevel'], trial_info['vevel'] = None, None, None, None
            while (len(behavioral_responses) > 0) and \
                    (behavioral_responses[-1]['trial_idx'] == trial_info['trial_idx']):
                response = behavioral_responses.pop()
                if response['response_id'] == 'HEPOS':
                    trial_info['hgpos'] = response['response_trace']
                elif response['response_id'] == 'VEPOS':
                    trial_info['vepos'] = response['response_trace']
                elif response['response_id'] == 'HEVEL':
                    trial_info['hevel'] = response['response_trace']
                elif response['response_id'] == 'VEVEL':
                    trial_info['vevel'] = response['response_trace']

            trial_info['events'] = dict()
            while (len(events) > 0) and (events[-1]['trial_idx'] == trial_info['trial_idx']):
                event_row = events.pop()
                trial_info['events'][event_row['event_ch']] = event_row['event_times']

            trial_info['spike_trains'] = dict()
            for unit_id in (unit_ids if isinstance(unit_ids, list) else []):
                u = units[unit_id]
                # IMPORTANT: A given unit may not have been recorded during a given trial.
                if (len(u) > 0) and u[-1]['trial_idx'] == trial_info['trial_idx']:
                    spike_times = u.pop()['spike_times']
                else:
                    spike_times = None
                trial_info['spike_trains'][unit_id] = spike_times

            trial_reps.append(TrialRep(trial_info))

        return trial_reps
    except Exception as e:
        get_application_logger().error(str(e), exc_info=True)
        return f"An internal error occured [{str(e)}]"


def retrieve_trial_reps_for_neuron(neuron_key: Dict[str, AttributeValue], proto_hash: str,
                                   complete_reps: bool = True) -> Union[str, List[TrialRep]]:
    """
    Retrieve data for all reps of the specified trial protocol during which the specified neural unit was recorded.
    NOTE that this method could take a significant amount of time depending on the number of trial reps that must be
    retrieved from the database.

    Args:
        neuron_key: At a minimum, this dictionary must uniquely identify a recorded neural unit in the database.
        proto_hash: The MD5 hash uniquely identifying a trial protocol in the database.
        complete_reps: If True, omit from result any trials that did not run to completion. Default = True.
    Returns:
        The list of trial reps retrieved, or an error description if the operation failed for any reason.
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
            return "Trial protocol not found"
        proto_def = maestro.Protocol.from_bytes(proto_info['proto_def'])

        # retrieve all relevant trials, and the neuronal and behavioral responses for those trials
        relevant_trials = fetch_restrict_proj([DBTable.TRIAL, DBTable.TRIAL_NEURONAL],
                                              [trial_restriction, neuron_pk], [])
        if relevant_trials is None:
            return "An internal error occurred while retrieving trials"
        neuronal_responses = fetch_restrict_proj([DBTable.TRIAL_NEURONAL, DBTable.TRIAL],
                                                 [neuron_pk, trial_restriction], [])
        if neuronal_responses is None:
            return "An internal error occurred while retrieving neural response data for trials"
        behavioral_responses = fetch_restrict_proj([DBTable.TRIAL_BEHAVIORAL, DBTable.TRIAL, DBTable.TRIAL_NEURONAL],
                                                   [None, trial_restriction, neuron_pk], [])
        if behavioral_responses is None:
            return "An internal error occurred while retrieving behavioral response data for trials"
        events = fetch_restrict_proj([DBTable.TRIAL_EVENT, DBTable.TRIAL], [None, trial_restriction], [])
        if events is None:
            return "An internal error occurred while retrieving event marker timestamps for trials"

        relevant_trials = sorted(relevant_trials, key=lambda k: k['trial_idx'], reverse=True)
        neuronal_responses = sorted(neuronal_responses, key=lambda k: k['trial_idx'], reverse=True)
        behavioral_responses = sorted(behavioral_responses, key=lambda k: k['trial_idx'], reverse=True)
        events = sorted(events, key=lambda k: k['trial_idx'], reverse=True)

        trial_reps: List[TrialRep] = list()
        while len(relevant_trials) > 0:
            trial_row = relevant_trials.pop()
            trial_info: Dict[str, Any] = dict()
            for k in trial_row.keys():
                trial_info[k] = trial_row[k]
            trial_info['session_date'] = str(trial_info['session_date'])  # want the date as an ISO-formatted string
            trial_info.pop('trial_header', None)  # don't need the trial header object
            trial_info['trial_rvs'] = json.loads(trial_info['trial_rvs'].decode())  # deserialize the RV values list
            trial_info['trial_success'] = (trial_info['trial_success'] != 0)   # DJ stores bool as int
            trial_info['trial_rewarded'] = (trial_info['trial_rewarded'] != 0)
            trial_info['protocol'] = proto_def
            trial_info.pop('proto_hash', None)

            trial_info['hgpos'], trial_info['vepos'], trial_info['hevel'], trial_info['vevel'] = None, None, None, None
            while (len(behavioral_responses) > 0) and \
                    (behavioral_responses[-1]['trial_idx'] == trial_info['trial_idx']):
                response = behavioral_responses.pop()
                if response['response_id'] == 'HEPOS':
                    trial_info['hgpos'] = response['response_trace']
                elif response['response_id'] == 'VEPOS':
                    trial_info['vepos'] = response['response_trace']
                elif response['response_id'] == 'HEVEL':
                    trial_info['hevel'] = response['response_trace']
                elif response['response_id'] == 'VEVEL':
                    trial_info['vevel'] = response['response_trace']

            trial_info['events'] = dict()
            while (len(events) > 0) and (events[-1]['trial_idx'] == trial_info['trial_idx']):
                event_row = events.pop()
                trial_info['events'][event_row['event_ch']] = event_row['event_times']

            neuronal_response = neuronal_responses.pop()
            trial_info['spike_trains'] = {neuronal_response['unit_id']: neuronal_response['spike_times']}

            trial_reps.append(TrialRep(trial_info))

        return trial_reps
    except Exception as e:
        get_application_logger().error(str(e), exc_info=True)
        return f"An internal error has occurred [{str(e)}]"
