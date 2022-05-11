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
Python dictionary or list of dictionaries, with each field described carefully so that users of this module can easily
manipulate the data returned in their analysis code.

The server-side implementation of the endpoints is found in the companion module endpoints.py. Another module,
data_containers.py, defines simple data containers for the various kinds of information that are retrieved by the API,
sent "over the wire" in pickled form, and reconstituted on the client side.

Author: saruffner
"""
import pickle
import time
from datetime import date
from typing import Optional, Union, List

import requests
from requests import RequestException, Response

from sglportalutils.data_containers import API_VERSION, SessionInfo, NeuronInfo, ROUTE_AUTHENTICATE, ROUTE_SESSIONINFO, \
    ROUTE_SESSION_NEURONS, ROUTE_SESSION_PROTOCOLS, TrialRep, ROUTE_SESSION_TRIAL, ROUTE_SESSION_BLOCK, \
    ROUTE_PROTOCOL_REPS
from sglportalutils.maestro import Protocol

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
            response = requests.post(f"{self._base_url}{ROUTE_AUTHENTICATE}",
                                     json=dict(username=self._uname, password=self._pwd),
                                     allow_redirects=False, timeout=_REQ_TIMEOUT_SECONDS)
            if response.status_code == 200 or response.status_code == 400:
                try:
                    content = pickle.loads(response.content)
                    if response.status_code == 200:
                        self._token = content['token']
                        self._expires = time.time() + content['expires_in'] - 60
                        return None
                    else:
                        return f"Authentication failed on server: [{response.status_code}] {content['error']}"
                except pickle.PickleError as e:
                    return f"Authentication failed: Error decoding response bytes - {str(e)}"
            return f"Authentication failed on server: [{response.status_code}] {response.reason}"
        except RequestException as e:
            return f"Authentication request failed on send: {str(e)}"

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
        response: Optional[Response] = None
        try:
            response = requests.post(f"{self._base_url}{ROUTE_SESSIONINFO}",
                                     json=req_body,
                                     headers={'Authorization': f"Bearer {self._token}"},
                                     allow_redirects=False, timeout=_REQ_TIMEOUT_SECONDS)
            content = pickle.loads(response.content)
            if response.status_code == 200:
                if content['version'] != API_VERSION:
                    return "Client side version mismatch with portal API; please update client side library."
                else:
                    return content['sessions']
            else:
                return content['error']
        except ValueError as e:
            return f"Retrieved session list invalid: {str(e)}"
        except pickle.PickleError:
            return f"Request for sessions list failed on server: [{response.status_code}] {response.reason}"
        except RequestException as e:
            return f"Request for sessions list failed on send: {str(e)}"

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
        response: Optional[Response] = None
        try:
            response = requests.post(f"{self._base_url}{ROUTE_SESSION_NEURONS}",
                                     json=req_body,
                                     headers={'Authorization': f"Bearer {self._token}"},
                                     allow_redirects=False, timeout=_REQ_TIMEOUT_SECONDS)
            content = pickle.loads(response.content)
            if response.status_code == 200:
                if content['version'] != API_VERSION:
                    return "Client side version mismatch with portal API; please update client side library."
                else:
                    return content['neurons']
            else:
                return content['error']
        except ValueError as e:
            return f"Retrieved session neurons list invalid: {str(e)}"
        except pickle.PickleError:
            return f"Request for session neurons list failed on server: [{response.status_code}] {response.reason}"
        except RequestException as e:
            return f"Request for session neurons list failed on send: {str(e)}"

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
        response: Optional[Response] = None
        try:
            response = requests.post(f"{self._base_url}{ROUTE_SESSION_PROTOCOLS}",
                                     json=req_body,
                                     headers={'Authorization': f"Bearer {self._token}"},
                                     allow_redirects=False, timeout=_REQ_TIMEOUT_SECONDS)
            content = pickle.loads(response.content)
            if response.status_code == 200:
                if content['version'] != API_VERSION:
                    return "Client side version mismatch with portal API; please update client side library."
                else:
                    return content['protocols']
            else:
                return content['error']
        except ValueError as e:
            return f"Retrieved session protocols list invalid: {str(e)}"
        except pickle.PickleError:
            return f"Request for session protocols list failed on server: [{response.status_code}] {response.reason}"
        except RequestException as e:
            return f"Request for session protocols list failed on send: {str(e)}"

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
        response: Optional[Response] = None
        try:
            response = requests.post(f"{self._base_url}{ROUTE_SESSION_TRIAL}",
                                     json=req_body,
                                     headers={'Authorization': f"Bearer {self._token}"},
                                     allow_redirects=False, timeout=_REQ_TIMEOUT_SECONDS)
            content = pickle.loads(response.content)
            if response.status_code == 200:
                if content['version'] != API_VERSION:
                    return "Client side version mismatch with portal API; please update client side library."
                else:
                    return content['trial']
            else:
                return content['error']
        except ValueError as e:
            return f"Retrieved session trial rep invalid: {str(e)}"
        except pickle.PickleError:
            return f"Request for a session trial rep failed on server: [{response.status_code}] {response.reason}"
        except RequestException as e:
            return f"Request for a session trial rep failed on send: {str(e)}"

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
        response: Optional[Response] = None
        try:
            response = requests.post(f"{self._base_url}{ROUTE_SESSION_BLOCK}",
                                     json=req_body,
                                     headers={'Authorization': f"Bearer {self._token}"},
                                     allow_redirects=False, timeout=_REQ_TIMEOUT_SECONDS)
            content = pickle.loads(response.content)
            if response.status_code == 200:
                if content['version'] != API_VERSION:
                    return "Client side version mismatch with portal API; please update client side library."
                else:
                    return content['trials']
            else:
                return content['error']
        except ValueError as e:
            return f"Request for a trial block failed: {str(e)}"
        except pickle.PickleError:
            return f"Request for a trial block failed on server: [{response.status_code}] {response.reason}"
        except RequestException as e:
            return f"Request for a trial block failed on send: {str(e)}"

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
        response: Optional[Response] = None
        try:
            response = requests.post(f"{self._base_url}{ROUTE_PROTOCOL_REPS}",
                                     json=req_body,
                                     headers={'Authorization': f"Bearer {self._token}"},
                                     allow_redirects=False, timeout=_REQ_TIMEOUT_SECONDS)
            content = pickle.loads(response.content)
            if response.status_code == 200:
                if content['version'] != API_VERSION:
                    return "Client side version mismatch with portal API; please update client side library."
                else:
                    return content['trials']
            else:
                return content['error']
        except ValueError as e:
            return f"Request for trial protocol reps failed: {str(e)}"
        except pickle.PickleError:
            return f"Request for trial protocol reps failed on server: [{response.status_code}] {response.reason}"
        except RequestException as e:
            return f"Request for trial protocol reps failed on send: {str(e)}"


def run_tests() -> None:
    accessor = PortalAccessor(username='sruffner', password='coltrane7Xy', url='http://localhost:8050')
    out = accessor.sessions(experimenter='nhall')
    if isinstance(out, str):
        print(f"sessions() call failed: {out}\n", flush=True)
        return
    elif len(out) == 0:
        print("sessions() call returned no session records\n", flush=True)
        return
    session = out[0]
    print(f"First session record: \n{session}\n", flush=True)

    out = accessor.session_neurons(session, min_spikes=20000)
    if isinstance(out, str):
        print(f"session_neurons() call failed: {out}\n", flush=True)
        return
    elif len(out) == 0:
        print("session_neurons() call returned no neuron records\n", flush=True)
        return
    print(f"{len(out)} neuron records retrieved:\n")
    for info in out:
        print(f"   {info}\n")

    out = accessor.session_protocols(session)
    if isinstance(out, str):
        print(f"session_protocols() call failed: {out}\n", flush=True)
        return
    print(f"{len(out)} trial protocols retrieved:\n")
    save_proto: Optional[Protocol] = None  # later we'll retrieve all reps of a selected protocol...
    for proto in out:
        print(f"  {proto.trial.path_name()}: {len(proto.trial.targets)} targets, {len(proto.trial.segments)} segs\n")
        if proto.trial.path_name().endswith('90-rtStab'):
            save_proto = proto

    out = accessor.session_trial(session, trial_idx=30, unit_ids=[13, 53, 58, 116])
    if isinstance(out, str):
        print(f"session_trial() call failed: {out}\n", flush=True)
        return
    print(f"Trial rep retrieved:\n{str(out)}")

    t_start = time.time()
    dur = 0
    for idx in range(100):
        out = accessor.session_trial(session, trial_idx=1000+idx, unit_ids=[13, 53, 58, 116])
        if isinstance(out, str):
            print(f"session_trial() call failed: {out}\n", flush=True)
            return
        dur = dur + out.duration
    t_elapsed = time.time() - t_start
    print(f"Retrieved 100 trial reps (1000-1099) singly in {t_elapsed:.3f} seconds; {t_elapsed/100.0:.3f} sec/trial. "
          f"Accumulated dur = {dur} ms.\n")

    t_start = time.time()
    out = accessor.session_trial_block(session, 1, 500, [28, 53, 58, 89, 118])
    t_elapsed = time.time() - t_start
    if isinstance(out, str):
        print(f"session_trial_block() failed: {out}\n", flush=True)
        return
    print(f"Retrieved 500-trial block in {t_elapsed:.3f} seconds ({t_elapsed/500.0:.3f} sec/trial:\n")
    for rep in out:
        print(f"  {rep.index}: {rep.protocol.trial.path_name()} [dur={rep.duration}]\n")
    print("\n", flush=True)

    if save_proto is not None:
        t_start = time.time()
        out = accessor.session_protocol_reps(session, save_proto, completed=True, unit_ids=[28, 53, 58, 89, 118])
        t_elapsed = time.time() - t_start
        if isinstance(out, str):
            print(f"session_protocol_reps() failed: {out}\n", flush=True)
            return
        print(f"Retrieved {len(out)} completed trial reps for protocol {save_proto.trial.path_name()} "
              f"in {t_elapsed:.3f} seconds; {t_elapsed/len(out):.3f} sec/trial\n", flush=True)

    print("\n\nDone!\n", flush=True)
