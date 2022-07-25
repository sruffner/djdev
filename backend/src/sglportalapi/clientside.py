"""
clientside.py: Client-side functions to access the Lisberger lab portal database via API endpoints.

The portal database is primarily an archive for experimental data collected in the Lisberger laboratory. While the web
portal offers graphical views of the data, including aggregate response statistics across repeated trial presentations,
it cannot possibly provide a full analytical suite.

Lab researchers would like to be able to retrieve preprocessed trial data from the database and perform their own
analyses. RESTful-like API endpoints are available on the portal server, providing a "read-only" avenue to retrieve
arbitrary experimental data sets. To protect data provenance, access to these endpoints requires user authentication
through a dedicated endpoint, which returns an access token that is supplied in requests to all other API endpoints.

This module is essentially a client-side "wrapper" for the API endpoints, intended for use by custom analysis scripts
-- or within an interactive Python console. It takes care of the details of user authentication, managing the access
token, preparing and sending the requests to the API and processing the responses. The data returned is generally a
data object or list of objects. The data object classes are defined in the modules data_containers.py; Maestro-specific
data objects such as trial protocol definitions and target definitions are defined in maestro.py.

The server-side implementation of the endpoints is found in the companion module endpoints.py.

Author: saruffner
"""
import time
from datetime import date
from typing import Optional, Union, List, Tuple

import requests
from requests import RequestException

from sglportalapi.data_containers import SessionInfo, NeuronInfo, TrialRep, Route, deserialize_api_response, \
    APISerializeError, MetadataTable
from sglportalapi.maestro import Protocol

_REQ_TIMEOUT_SECONDS: float = 20
""" Any request to a portal API endpoint will timeout after this many seconds. """


class PortalAccessor:
    def __init__(self, username: str, password: str, url: str):
        """
        Create an accessor object that manages all queries to the Lisberger lab portal database via RESTful-like API
        endpoints.

        Args:
            username: Username of a registered user on the portal.
            password: The registered user's password.
            url: The portal's root URL -- where the portal web application is accessed on a browser. All API endpoints
                are path extensions of this URL.
        """
        self._uname: str = username
        """ Username for authentication on portal. """
        self._pwd: str = password
        """ Password for authentication on portal. """
        self._base_url: str = url[:-1] if url.endswith('/') else url
        """ The portal's root URL."""
        self._token: Optional[str] = None
        """ Access token required for portal API access; obtained upon authenticating user at root endpoint. """
        self._expires: Optional[float] = None
        """ Approximate time at which access token expires, in seconds since the epoch."""

    def authenticate(self) -> Optional[str]:
        """
        Authenticate user with the portal API, obtaining a JSON web token as an access token that must be supplied in
        all future requests to retrieve portal data through API endpoints.

        Returns:
            None if authentication was successful (or user is already authenticated); else an error message. Reasons for
                failure include connection issues, invalid credentials, or internal server errors.
        """
        # authenticate if we don't have an access token, or token has expired
        if isinstance(self._token, str):
            if time.time() >= self._expires:
                self._token, self._expires = None, None
            else:
                return None  # already authenticated

        try:
            response = requests.post(f"{self._base_url}{Route.AUTHENTICATE}",
                                     json=dict(username=self._uname, password=self._pwd),
                                     allow_redirects=False, timeout=_REQ_TIMEOUT_SECONDS)
            if response.status_code == 200 or response.status_code == 400:
                try:
                    content = deserialize_api_response(Route.AUTHENTICATE, response.content)
                    if response.status_code == 200:
                        self._token = content['token']
                        self._expires = time.time() + content['expires_in'] - 60
                        return None
                    else:
                        return f"Authentication failed on server: [{response.status_code}] {content['error']}"
                except APISerializeError as e:
                    return f"Authentication failed; unable to decode server response: {str(e)}"
            return f"Authentication failed on server: [{response.status_code}] {response.reason}"
        except RequestException as e:
            return f"Authentication request failed on send: {str(e)}"

    def metadata_table(self, table_name: str) -> Tuple[str, Optional[MetadataTable]]:
        """
        Retrieve the entire contents of one of the small "general information" tables in the portal database. These
        tables contain information used to describe, categorize and search for experimental data sets, such as:
        experiment subjects, subject implants, experiment rigs, research studies, neuron types, and brain areas. The
        very large database tables storing experiment sessions, neural units, trial protocols, and recorded trial
        response data are **not** exposed by this method.

        Args:
            table_name: Name of the metadata table requested. For a list of recognized table names, see
                :py:class:`sglportalapi.data_containers.MetadataTable`.
        Returns:
            A 3-tuple (emsg, metatable). On failure, `emsg` is the error description and `metatable` is None. On
                success, emsg is an empty string, and `metatable' encapsulates the requested table's contents.
        """
        if (out := self.authenticate()) is not None:
            return out, None
        req_body = dict(table=table_name)
        try:
            response = requests.post(f"{self._base_url}{Route.METADATA_TABLE}",
                                     json=req_body,
                                     headers={'Authorization': f"Bearer {self._token}"},
                                     allow_redirects=False, timeout=_REQ_TIMEOUT_SECONDS)
            content = deserialize_api_response(Route.METADATA_TABLE, response.content)
            if response.status_code == 200:
                return '', content['metatable']
            else:
                return f"Request failed on server [{response.status_code}]: {content['error']}", None
        except APISerializeError as e:
            return f"Failed to decode server response: {str(e)}", None
        except RequestException as e:
            return f"Request failed on send: {str(e)}", None

    def sessions(self, experimenter: Optional[str] = None, subject: Optional[str] = None,
                 when: Optional[str] = None) -> Union[str, List[SessionInfo]]:
        """
        Retrieve a list of experiment sessions from the portal database.

        Args:
            experimenter: If not None, restrict to sessions belonging to the registered portal user identified by this
                username.
            subject: If not None, restrict to sessions belonging to the experiment subject with this ID.
            when: If not None, restrict to sessions with a recording date that satisfies this condition. It must have
                the form 'op YYYY-MM-DD', where 'op' is one of '=' (on specified date), '>' (after specified date), or
                '<' (before specified date).
        Returns:
            If successful, a list (possibly empty) of session information objects for all experiment sessions in the
                portal database that satisfy the given constraints. If the operation fails, returns an error message.
        Raises:
            ValueError:
                If the 'when' argument is incorrectly formatted.
        """
        if (out := self.authenticate()) is not None:
            return out
        if when is not None:
            when_parts = when.split()
            try:
                if not ((len(when_parts) == 2) and (when_parts[0] in ['=', '>', '<'])):
                    raise ValueError()
                date.fromisoformat(when_parts[1])
            except Exception:
                raise ValueError("Arg 'when' must have the format '=|>|< YYYY-mm-dd'")
        req_body = dict(experimenter=experimenter, subj_id=subject, when=when)
        try:
            response = requests.post(f"{self._base_url}{Route.SESSIONINFO}",
                                     json=req_body,
                                     headers={'Authorization': f"Bearer {self._token}"},
                                     allow_redirects=False, timeout=_REQ_TIMEOUT_SECONDS)
            content = deserialize_api_response(Route.SESSIONINFO, response.content)
            if response.status_code == 200:
                return content['sessions']
            else:
                return f"Request failed on server [{response.status_code}]: {content['error']}"
        except APISerializeError as e:
            return f"Failed to decode server response: {str(e)}"
        except RequestException as e:
            return f"Request failed on send: {str(e)}"

    def session_neurons(self, session: SessionInfo, min_spikes: Optional[int] = None, min_snr: Optional[float] = None) \
            -> Union[str, List[NeuronInfo]]:
        """
        Retrieve information about selected neural units recorded during a specified experiment session committed to
        the Lisberger lab portal database.

        Args:
            session: The experiment session.
            min_spikes: If specified, retrieve only those units with a total recorded spike count matching or exceeding
                this limit. Default = None.
            min_snr: If specified, retrieve only those units with an estimated signal-to-noise value matching or
                exceeding this threshold. Default = None.
        Returns:
             If successful, a list (possibly empty) of neuron information records for all neural units recorded during
                the session that satisfy the given constraints. If the operation fails, returns an error message.
        """
        if (out := self.authenticate()) is not None:
            return out
        req_body = dict(session_key=session.primary_key, min_spikes=min_spikes, min_snr=min_snr)
        try:
            response = requests.post(f"{self._base_url}{Route.SESSION_NEURONS}",
                                     json=req_body,
                                     headers={'Authorization': f"Bearer {self._token}"},
                                     allow_redirects=False, timeout=_REQ_TIMEOUT_SECONDS)
            content = deserialize_api_response(Route.SESSION_NEURONS, response.content)
            if response.status_code == 200:
                return content['neurons']
            else:
                return f"Request failed on server [{response.status_code}]: {content['error']}"
        except APISerializeError as e:
            return f"Failed to decode server response: {str(e)}"
        except RequestException as e:
            return f"Request failed on send: {str(e)}"

    def session_protocols(self, session: SessionInfo) -> Union[str, List[Protocol]]:
        """
        Retrieve the definitions of all distinct Maestro trial protocols presented during a specified experiment session
        committed to the Lisberger lab portal database.

        Args:
            session: The experiment session.
        Returns:
             If successful, a list of protocol definition objects. If the operation fails, returns an error message.
        """
        if (out := self.authenticate()) is not None:
            return out
        req_body = dict(session_key=session.primary_key)
        try:
            response = requests.post(f"{self._base_url}{Route.SESSION_PROTOCOLS}",
                                     json=req_body,
                                     headers={'Authorization': f"Bearer {self._token}"},
                                     allow_redirects=False, timeout=_REQ_TIMEOUT_SECONDS)
            content = deserialize_api_response(Route.SESSION_PROTOCOLS, response.content)
            if response.status_code == 200:
                return content['protocols']
            else:
                return f"Request failed on server [{response.status_code}]: {content['error']}"
        except APISerializeError as e:
            return f"Failed to decode server response: {str(e)}"
        except RequestException as e:
            return f"Request failed on send: {str(e)}"

    def session_trial(self, session: SessionInfo, trial_idx: int, unit_ids: Optional[List[int]] = None) -> \
            Union[str, TrialRep]:
        """
        Retrieve response data and other metadata for a single trial rep recorded during a specified experiment session.

        Args:
            session: The experiment session.
            trial_idx: The index of the trial to retrieve. This is an integer in [1..N], where N is the number of trials
                presented during the experiment.
            unit_ids: The IDs of up to 5 neural units for which trial-aligned spike train responses are requested. Each
                ID is an integer in [1..M], where M is the number of distinct units recorded during the experiment. If
                None or empty list, no neural response data is retrieved. Only the first 5 unique IDs are included; any
                additional or repeat elements are ignored.
        Returns:
            The trial rep requested, or an error message if the operation fails.
        """
        if (out := self.authenticate()) is not None:
            return out
        unit_ids = sorted([x for x in set(unit_ids)]) if isinstance(unit_ids, list) else []
        req_body = dict(session_key=session.primary_key, trial_index=trial_idx, unit_ids=unit_ids[0:5])
        try:
            response = requests.post(f"{self._base_url}{Route.SESSION_TRIAL}",
                                     json=req_body,
                                     headers={'Authorization': f"Bearer {self._token}"},
                                     allow_redirects=False, timeout=_REQ_TIMEOUT_SECONDS)
            content = deserialize_api_response(Route.SESSION_TRIAL, response.content)
            if response.status_code == 200:
                return content['trial']
            else:
                return f"Request failed on server [{response.status_code}]: {content['error']}"
        except APISerializeError as e:
            return f"Failed to decode server response: {str(e)}"
        except RequestException as e:
            return f"Request failed on send: {str(e)}"

    def session_trial_block(self, session: SessionInfo, start: int, end: int, unit_ids: Optional[List[int]] = None) -> \
            Union[str, List[TrialRep]]:
        """
        Retrieve response data and other metadata for a sequential block of up to 25 trial reps recorded during a
        specified experiment session.

        Args:
            session: The experiment session.
            start: The index of the first trial in block. This is an integer in [1..N], where N is the number of trials
                presented during the experiment.
            end: Index of last trial in block. Must be >= 'start'.
            unit_ids: The IDs of up to 5 neural units for which trial-aligned spike train responses are requested. Each
                ID is an integer in [1..M], where M is the number of distinct units recorded during the experiment. If
                None or empty list, no neural response data is retrieved. Only the first 5 unique IDs are included; any
                additional or repeat elements are ignored.
        Returns:
            The list of trials requested, or an error message if the operation fails.
        """
        if (out := self.authenticate()) is not None:
            return out
        unit_ids = sorted([x for x in set(unit_ids)]) if isinstance(unit_ids, list) else []
        req_body = dict(session_key=session.primary_key, start=start, end=end, unit_ids=unit_ids[0:5])
        try:
            response = requests.post(f"{self._base_url}{Route.SESSION_BLOCK}",
                                     json=req_body,
                                     headers={'Authorization': f"Bearer {self._token}"},
                                     allow_redirects=False, timeout=_REQ_TIMEOUT_SECONDS)
            content = deserialize_api_response(Route.SESSION_BLOCK, response.content)
            if response.status_code == 200:
                return content['trials']
            else:
                return f"Request failed on server [{response.status_code}]: {content['error']}"
        except APISerializeError as e:
            return f"Failed to decode server response: {str(e)}"
        except RequestException as e:
            return f"Request failed on send: {str(e)}"

    def session_protocol_reps(self, session: SessionInfo, proto: Protocol, completed: bool = False,
                              unit_ids: Optional[List[int]] = None) -> Union[str, List[TrialRep]]:
        """
        Retrieve response data and other metadata for all reps of a specified Maestro trial protocol recorde during a
        specified experiment session.

        Args:
            session: The experiment session.
            proto: The Maestro trial protocol.
            completed: If False (True), all (only successfully completed) reps of the specified protocol are included
                in the result. Default = False.
            unit_ids: The IDs of up to 5 neural units for which trial-aligned spike train responses are requested. Each
                ID is an integer in [1..M], where M is the number of distinct units recorded during the experiment. If
                None or empty list, no neural response data is retrieved. Only the first 5 unique IDs are included; any
                additional or repeat elements are ignored.
        Returns:
            The list of trials requested, or an error message if the operation fails.
        """
        if (out := self.authenticate()) is not None:
            return out
        unit_ids = sorted([x for x in set(unit_ids)]) if isinstance(unit_ids, list) else []
        req_body = dict(session_key=session.primary_key, proto_hash=proto.md5_digest, completed=completed,
                        unit_ids=unit_ids[0:5])
        try:
            response = requests.post(f"{self._base_url}{Route.SESSION_PROTOCOL_REPS}",
                                     json=req_body,
                                     headers={'Authorization': f"Bearer {self._token}"},
                                     allow_redirects=False, timeout=_REQ_TIMEOUT_SECONDS)
            content = deserialize_api_response(Route.SESSION_PROTOCOL_REPS, response.content)
            if response.status_code == 200:
                return content['trials']
            else:
                return f"Request failed on server [{response.status_code}]: {content['error']}"
        except APISerializeError as e:
            return f"Failed to decode server response: {str(e)}"
        except RequestException as e:
            return f"Request failed on send: {str(e)}"
