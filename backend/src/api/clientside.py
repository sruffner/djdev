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

The server-side implementation of the endpoints is found in the companion module endpoints.py.

Author: saruffner
"""
import pickle
import time
from datetime import date
from typing import Optional, Union, List

import requests
from requests import RequestException, Response

from api.data_containers import API_VERSION, SessionInfo

_REQ_TIMEOUT_SECONDS: float = 20
""" Any request to a portal API endpoint will timeout after this many seconds. """
_ROUTE_AUTHENTICATE: str = "api"
_ROUTE_SESSIONINFO: str = "api/sessions"


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
        self._base_url: str = url if url.endswith('/') else f"{url}/"
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
            response = requests.post(f"{self._base_url}{_ROUTE_AUTHENTICATE}",
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
            response = requests.post(f"{self._base_url}{_ROUTE_SESSIONINFO}",
                                     json=req_body,
                                     headers={'Authorization': f"Bearer {self._token}"},
                                     allow_redirects=False, timeout=_REQ_TIMEOUT_SECONDS)
            content = pickle.loads(response.content)
            if response.status_code == 200:
                if content['version'] != API_VERSION:
                    return "Client side version mismatch with portal API; please update client side library."
                else:
                    return [SessionInfo(info) for info in content['sessions']]
        except ValueError as e:
            return f"Retrieved session list invalid: {str(e)}"
        except pickle.PickleError:
            return f"Request failed on server: [{response.status_code}] {response.reason}"
        except RequestException as e:
            return f"Request for sessions list failed on send: {str(e)}"
