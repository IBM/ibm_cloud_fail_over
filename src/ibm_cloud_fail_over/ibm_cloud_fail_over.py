#!/usr/bin/env python3

# Copyright 2025 Eran Gampel
# Authors:      Eran Gampel , Jorge Hernández Ramírez
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""IBM Cloud Fail Over module.

This module provides functions for handling failover operations in IBM Cloud VPC.
"""

import http.client
import json
import sys
import socket
from ipaddress import ip_network
from typing import Tuple, Optional
from os import environ as env
from dotenv import load_dotenv
from ibm_cloud_sdk_core import ApiException


load_dotenv("env")

class HAFailOver():
    """IBM Cloud Fail Over handler class.

    This class provides methods for handling failover operations in IBM Cloud VPC.
    """
    API_KEY = "API_KEY"
    VPC_ID = "VPC_ID"
    VPC_URL = "VPC_URL"
    ZONE = "ZONE"
    VSI_LOCAL_AZ = "VSI_LOCAL_AZ"
    EXT_IP_1 = "EXT_IP_1"
    EXT_IP_2 = "EXT_IP_2"
    METADATA_VERSION = "2022-03-01"
    METADATA_HOST = "api.metadata.cloud.ibm.com"
    METADATA_PATH = "/instance_identity/v1/"
    METADATA_INSTACE_PATH = "/metadata/v1/instance"
    METADATA_INSTACE_NETWORK_INT_PATH = "/metadata/v1/instance/network_interfaces"
    METADATA_VNI_PATH = "/metadata/v1/virtual_network_interfaces"
    API_VERSION = "2026-06-01"
    apikey = None
    vpc_url = ""
    vpc_id = ""
    table_id = ""
    route_id = ""
    zone = ""
    next_hop_vsi = ""
    update_next_hop_vsi = ""
    ext_ip_1 = ""
    ext_ip_2 = ""
    vsi_local_az = ""
    DEBUG = False
    #DEBUG = True

    def __init__(self) -> None:
        """Initialize the HAFailOver instance."""
        self.logger("--------Constructor---------")
        if self.apikey is None:
            self.logger("--------_parse_config")
            self._parse_config()

    def _make_api_request(self, method: str, path: str, headers: dict = None, body: str = None) -> dict:
        """Make an API request to the VPC API.

        Args:
            method (str): HTTP method (GET, POST, PATCH, DELETE)
            path (str): API path
            headers (dict, optional): Additional headers. Defaults to None.
            body (str, optional): Request body. Defaults to None.

        Returns:
            dict: API response

        Raises:
            ApiException: If the API request fails
        """
        conn = None
        try:
            vpc_host = self.vpc_url.replace('https://', '')
            conn = http.client.HTTPSConnection(vpc_host)

            # Set up default headers
            default_headers = {
                'Authorization': self.get_token(),
                'Content-Type': 'application/json',
                'X-IBM-Cloud-API-Version': self.API_VERSION,
                'X-IBM-Cloud-Generation': '2'
            }

            # Merge with additional headers if provided
            if headers:
                default_headers.update(headers)

            # Make the request
            conn.request(method, path, body=body, headers=default_headers)
            response = conn.getresponse()

            # Read and parse response
            response_data = response.read().decode("utf-8")

            # Handle successful responses
            if response.status in [200, 201, 204]:
                # For DELETE operations, 204 No Content is a success
                if response.status == 204:
                    return {}
                return json.loads(response_data) if response_data else {}

            # Handle error responses - always raise exception for non-success status
            raise ApiException(f"API request failed: {response.status} {response.reason}\nResponse: {response_data}")

        except ApiException:
            raise
        except Exception as e:
            raise ApiException(f"Error making API request: {str(e)}") from e
        finally:
            if conn is not None:
                conn.close()

    def update_vpc_fip(self, cmd, vni_id, fip_id):
        """Update floating IP attachment.

        Args:
            cmd (str): 'add' or 'remove'
            vni_id (str): VNI ID
            fip_id (str): Floating IP ID
        """
        self.logger("Calling update vpc routing table route method VIP.")
        self.logger(f"VPC ID: {self.vpc_id}")
        self.logger(f"VPC URL: {self.vpc_url}")
        self.logger(f"cmd: {cmd}")

        try:
            if cmd == "remove":
                self._make_api_request(
                    "DELETE",
                    f"/v1/network_interfaces/{vni_id}/floating_ips/{fip_id}?version={self.API_VERSION}&generation=2"
                )
            if cmd == "add":
                self._make_api_request(
                    "PUT",
                    f"/v1/network_interfaces/{vni_id}/floating_ips/{fip_id}?version={self.API_VERSION}&generation=2"
                )
            return True
        except ApiException as e:
            self.logger(f"Error updating floating IP: {e}")
            raise

    def update_vpc_routing_table_route(self, cmd, ingress_types=None):
        """Update VPC routing table route.

        Args:
            cmd (str): 'SET' or 'GET'
            ingress_types (list, optional): List of ingress types to update. Can include:
                - 'route_internet_ingress'
                - 'route_direct_link_ingress'
                - 'route_transit_gateway_ingress'
                Defaults to ['route_direct_link_ingress', 'route_transit_gateway_ingress', 'route_internet_ingress']

        Returns:
            str: Updated next hop IP
        """
        self.logger("Calling update vpc routing table route method VIP.")
        self.logger(f"VPC ID: {self.vpc_id}")
        self.logger(f"VPC URL: {self.vpc_url}")
        self.logger(f"VPC self.ext_ip_1: {self.ext_ip_1}")
        self.logger(f"VPC self.ext_ip_2: {self.ext_ip_2}")
        self.logger(f"Command: {cmd}")
        self.logger(f"Ingress types to update: {ingress_types}")

        # Set default ingress types if none provided
        if ingress_types is None:
            ingress_types = ['route_direct_link_ingress', 'route_transit_gateway_ingress', 'route_internet_ingress']
        self.logger(f"Using ingress types: {ingress_types}")

        try:
            # Get all routing tables
            self.logger("Getting routing tables...")
            list_tables = self._make_api_request(
                "GET",
                f"/v1/vpcs/{self.vpc_id}/routing_tables?version={self.API_VERSION}&generation=2"
            )

            if not list_tables or "routing_tables" not in list_tables:
                raise ApiException(f"No routing tables found for VPC {self.vpc_id}")

            # Process each routing table
            for table in list_tables["routing_tables"]:
                # Check if this is one of the specified ingress routing tables

                is_ingress_table = any(table.get(ingress_type) for ingress_type in ingress_types)
                if True:
                    self.logger(f"Found matching ingress routing table: {table['name']} (ID: {table['id']})")
                    self.logger(f"Table type: internet_ingress={table.get('route_internet_ingress')}, "
                              f"direct_link_ingress={table.get('route_direct_link_ingress')}, "
                              f"transit_gateway_ingress={table.get('route_transit_gateway_ingress')}")
                    table_id = table["id"]

                    # Get routes for this table
                    self.logger(f"Getting routes for table {table_id}...")
                    routes = self._make_api_request(
                        "GET",
                        f"/v1/vpcs/{self.vpc_id}/routing_tables/{table_id}/routes?version={self.API_VERSION}&generation=2"
                    )["routes"]

                    # Process each route
                    for route in routes:
                        self.logger(f"Checking route: {route['name']} (ID: {route['id']})")
                        self.logger(f"Route details - destination: {route['destination']}, zone: {route['zone']['name']}, next_hop: {route['next_hop']['address']}")

                        if route["next_hop"]["address"] in [self.ext_ip_1, self.ext_ip_2]:
                            if cmd == "GET":
                                self.logger(f"GET command - returning current next hop: {route['next_hop']['address']}")
                                return route["next_hop"]["address"]

                            self.find_the_current_and_next_hop_ip(route["next_hop"]["address"])
                            self.logger(f"Route update - current hop: {self.next_hop_vsi}, new hop: {self.update_next_hop_vsi}")

                            # Update or create route based on zone
                            if route["zone"]["name"] == self.vsi_local_az or not is_ingress_table:
                                self.logger(f"Updating existing route in zone {route['zone']['name']}")
                                # Update existing route
                                route_patch = {
                                    "advertise": route["advertise"],
                                    "name": route["name"],
                                    "next_hop": {"address": self.update_next_hop_vsi},
                                    "priority": route["priority"]
                                }

                                self.logger(f"Patching route {route['id']} with data: {route_patch}")
                                self._make_api_request(
                                    "PATCH",
                                    f"/v1/vpcs/{self.vpc_id}/routing_tables/{table_id}/routes/{route['id']}?version={self.API_VERSION}&generation=2",
                                    body=json.dumps(route_patch)
                                )
                                self.logger(f"Successfully updated route {route['id']} to use next hop {self.update_next_hop_vsi}")
                            else:
                                self.logger(f"Route is in different zone ({route['zone']['name']}), creating new route in zone {self.vsi_local_az}")
                                # Delete and create new route
                                self.logger(f"Deleting route {route['id']} from zone {route['zone']['name']}")
                                self._make_api_request(
                                    "DELETE",
                                    f"/v1/vpcs/{self.vpc_id}/routing_tables/{table_id}/routes/{route['id']}?version={self.API_VERSION}&generation=2"
                                )

                                new_route = {
                                    "destination": route["destination"],
                                    "zone": {"name": self.vsi_local_az} if self.vsi_local_az else route["zone"],
                                    "action": "deliver",
                                    "next_hop": {"address": self.update_next_hop_vsi},
                                    "name": route["name"],
                                    "advertise": route["advertise"]
                                }

                                self.logger(f"Creating new route with data: {new_route}")
                                self._make_api_request(
                                    "POST",
                                    f"/v1/vpcs/{self.vpc_id}/routing_tables/{table_id}/routes?version={self.API_VERSION}&generation=2",
                                    body=json.dumps(new_route)
                                )
                                self.logger(f"Successfully created new route with next hop {self.update_next_hop_vsi}")

            self.logger(f"Returning updated next hop: {self.update_next_hop_vsi}")
            return self.update_next_hop_vsi

        except ApiException as e:
            self.logger(f"Error updating routing table route: {e}")
            raise

    def get_token(self):
        """Get Token

        Returns:
            string: Returning the access token

        """
        if self.apikey is not None:
            self.logger("------apikey path")
            return self._get_token_from_apikey()
        self.logger("------trusted profile path")
        return self._get_token_from_tp()

    def _get_token_from_tp(self):
        """_summary_

        Returns:
            _type_: _description_
        """
        connection = self._get_metadata_connection()
        return self._get_iam_token_from_tp(connection)

    def _get_iam_token_from_tp(self, connection: http.client.HTTPSConnection):
        """_summary_

        Args:
            connection (http.client.HTTPSConnection): _description_

        Raises:
            ApiException: _description_

        Returns:
            _type_: _description_
        """
        metadata_token = self._get_metadata_token(connection)
        connection.request("POST",
                           self._get_metadata_iam_token_path(),
                           headers=self._get_metadata_headers_iam(metadata_token))
        response = json.loads(connection.getresponse().read().decode("utf-8"))
        if 'access_token' not in response:
            self.logger(response)
            self.logger('Can not get access token from trusted profile.'
                        'Review if a TP is bound to the instance.')
            raise ApiException('Can not get access token from trusted profile.'
                            'Review if a TP is bound to the instance.')
        return f"Bearer {response['access_token']}"

    def _get_metadata_token(self, connection: http.client.HTTPSConnection):
        """_summary_

        Args:
            connection (http.client.HTTPSConnection): _description_

        Returns:
            _type_: _description_
        """
        connection.request("PUT",
                           self._get_metadata_token_path(),
                           body=self._get_metadata_body(),
                           headers=self._get_metadata_headers())
        return json.loads(connection.getresponse().read().decode("utf-8"))['access_token']

    def _get_metadata_token_path(self):
        """_summary_

        Returns:
            _type_: _description_
        """
        return f"{self.METADATA_PATH}token?version={self.METADATA_VERSION}"

    def _get_metadata_istance_network_int_path(self):
        """_summary_

        Returns:
            _type_: _description_
        """
        return f"{self.METADATA_INSTACE_NETWORK_INT_PATH}?version={self.METADATA_VERSION}"

    def _get_metadata_vni_path(self):
        """_summary_

        Returns:
            _type_: _description_
        """
        return f"{self.METADATA_VNI_PATH}?version={self.METADATA_VERSION}"



    def _get_metadata_istance_path(self):
        """_summary_

        Returns:
            _type_: _description_
        """
        return f"{self.METADATA_INSTACE_PATH}?version={self.METADATA_VERSION}"

    def _get_metadata_iam_token_path(self):
        """_summary_

        Returns:
            _type_: _description_
        """
        return f"{self.METADATA_PATH}iam_token?version={self.METADATA_VERSION}"

    def _get_metadata_body(self):
        """_summary_

        Returns:
            _type_: _description_
        """
        return json.dumps({
            "expires_in": 3600
        })

    def _get_metadata_headers(self) -> dict:
        """_summary_

        Returns:
            dict: _description_
        """
        return {
            'Metadata-Flavor': 'ibm',
            'Accept': 'application/json'
        }

    def _get_metadata_headers_iam(self, metadata_token) -> dict:
        """_summary_

        Args:
            metadata_token (_type_): _description_

        Returns:
            dict: _description_
        """
        headers = self._get_metadata_headers()
        headers['Authorization'] = f"Bearer {metadata_token}"
        return headers

    def _get_metadata_connection(self):
        """_summary_

        Raises:
            ApiException: _description_

        Returns:
            _type_: _description_
        """
        connection = None
        if self._check_connectivity(self.METADATA_HOST, 80):
            connection = http.client.HTTPConnection(self.METADATA_HOST)
        elif self._check_connectivity(self.METADATA_HOST, 443):
            connection = http.client.HTTPSConnection(self.METADATA_HOST)
        if connection is None:
            self.logger("Activate metadata at VSI instance please!"
                        "and be sure that a TP is bound to the instance")
            raise ApiException("Activate metadata at VSI instance please!"
                            "and be sure that a TP is bound to the instance")
        return connection

    def _get_token_from_apikey(self):
        """_summary_

        Returns:
            _type_: _description_
        """
        # URL for token
        conn = http.client.HTTPSConnection("private.iam.cloud.ibm.com")
        # Payload for retrieving token. Note: An API key will need to be generated and replaced here
        payload = (
            "grant_type=urn%3Aibm%3Aparams%3Aoauth%3Agrant-type%3Aapikey&apikey="
            + self.apikey
            + "&response_type=cloud_iam"
        )

        # Required headers
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "Cache-Control": "no-cache",
        }

        try:
            # Connect to endpoint for retrieving a token
            conn.request("POST", "/identity/token", payload, headers)

            # Get and read response data
            res = conn.getresponse().read()
            data = res.decode("utf-8")

            # Format response in JSON
            json_res = json.loads(data)

            # Concatenate token type and token value
            return json_res["token_type"] + " " + json_res["access_token"]

        # If an error happens while retrieving token
        except Exception as error:
            self.logger(f"Error getting token. {error}")
            raise

    def _check_connectivity(self, ip, port):
        """_summary_

        Args:
            ip (_type_): _description_
            port (_type_): _description_

        Returns:
            _type_: _description_
        """
        try:
            with socket.create_connection((ip, port), 5):
                self.logger(f"Successfully connected to {ip}:{port}")
                return True
        except socket.timeout:
            self.logger(f"Connection to {ip}:{port} timed out.")
        except socket.error as e:
            self.logger(f"Failed to connect to {ip}:{port}: {e}")
        return False

    def _parameter_exception(self, missing_parameter):
        """_parameter_exception
        Parameters:
        missing_parameter (string): Description of the missing parameter

        Returns:
        exception: raise an ApiException

        """
        raise ApiException("Please!!! provide " + missing_parameter)

    def _parse_config(self):
        """_parse_config

        Returns:

        """

        try:
            self.logger(env)
            if self.API_KEY in env:
                self.apikey = env[self.API_KEY]

            if self.VPC_ID in env:
                self.vpc_id = env[self.VPC_ID]
                self.vpc_id = env[self.VPC_ID]
                self.logger(self.VPC_ID + ": " + self.vpc_id)

            if self.VPC_URL in env:
                self.vpc_url = env[self.VPC_URL]
                self.logger(self.VPC_URL + ": " + self.vpc_url)
            else:
                self._parameter_exception(self.VPC_URL)

            if self.VSI_LOCAL_AZ in env:
                self.vsi_local_az = env[self.VSI_LOCAL_AZ]
                self.logger("VSI Local AZ: " + self.vsi_local_az)
            else:
                self.vsi_local_az = ""

            if self.EXT_IP_1 in env:
                self.ext_ip_1 = env[self.EXT_IP_1]
                self.logger("External IP 1: " + self.ext_ip_1)

            if self.EXT_IP_2 in env:
                self.ext_ip_2 = env[self.EXT_IP_2]
                self.logger("External IP 1: " + self.ext_ip_2)

        except ApiException as e:
            self.logger(e)

    def logger(self, message):
        """_summary_

        Args:
            message (_type_): _description_
        """
        if self.DEBUG:
            print(message)

    def find_the_current_and_next_hop_ip(self, route_address):
        """_summary_

        Args:
            route_address (_type_): _description_
        """
        if route_address == self.ext_ip_1:
            # To be updated with IP address.
            self.update_next_hop_vsi = self.ext_ip_2
            # Current Hop IP address.
            self.next_hop_vsi = self.ext_ip_1
        else:
            # To be updated with IP address.
            self.update_next_hop_vsi = self.ext_ip_1
            # Current IP address.
            self.next_hop_vsi = self.ext_ip_2
        self.logger("Current next hop IP is: " + self.next_hop_vsi)
        self.logger("Update next hop IP to: " + self.update_next_hop_vsi)

    def get_instance_metadata(self):
        connection = self._get_metadata_connection()
        metadata_token = self._get_metadata_token(connection)
        connection.request("GET",
                           self._get_metadata_istance_path(),
                           headers=self._get_metadata_headers_iam(metadata_token))
        response = json.loads(connection.getresponse().read().decode("utf-8"))
        return response

    def get_instance_interface_metadata(self):
        connection = self._get_metadata_connection()
        metadata_token = self._get_metadata_token(connection)
        connection.request("GET",
                           self._get_metadata_istance_network_int_path(),
                           headers=self._get_metadata_headers_iam(metadata_token))
        response = json.loads(connection.getresponse().read().decode("utf-8"))
        return response

    def get_vni_metadata(self):
        connection = self._get_metadata_connection()
        metadata_token = self._get_metadata_token(connection)
        connection.request("GET",
                           self._get_metadata_vni_path(),
                           headers=self._get_metadata_headers_iam(metadata_token))
        response = json.loads(connection.getresponse().read().decode("utf-8"))
        return response

    def get_next_hop_for_cidr(self, cidr: str, api_version: str = "2026-06-01",
                             maturity: Optional[str] = None, generation: str = "2") -> str:
        """Get the next hop IP address for a given CIDR in the internet ingress routing table.

        Args:
            cidr (str): The CIDR to search for in routing tables
            api_version (str, optional): API version to use. Defaults to "2026-06-01".
            maturity (str, optional): API maturity level. Defaults to None (omitted for GA APIs).
            generation (str, optional): API generation. Defaults to "2".

        Returns:
            str: The next hop IP address for the route matching the CIDR, or None if not found

        Raises:
            ApiException: If there is an error getting the next hop
        """
        self.logger(f"Getting next hop for CIDR: {cidr}")
        self.logger(f"VPC ID: {self.vpc_id}")
        self.logger(f"VPC URL: {self.vpc_url}")

        try:
            # Get all routing tables
            maturity_param = f"&maturity={maturity}" if maturity else ""
            list_tables = self._make_api_request(
                "GET",
                f"/v1/vpcs/{self.vpc_id}/routing_tables?version={api_version}&generation={generation}{maturity_param}"
            )

            if not list_tables or "routing_tables" not in list_tables:
                raise ApiException(f"No routing tables found for VPC {self.vpc_id}")

            # Search through routing tables
            for table in list_tables["routing_tables"]:
                # Check if this is an internet ingress routing table
                if table.get("route_internet_ingress"):
                    self.logger(f"Found internet ingress routing table: {table['name']} (ID: {table['id']})")
                    table_id = table["id"]

                    # Get all routes in this table
                    routes = self._make_api_request(
                        "GET",
                        f"/v1/vpcs/{self.vpc_id}/routing_tables/{table_id}/routes?version={api_version}&generation={generation}{maturity_param}"
                    )["routes"]

                    # Check each route for the CIDR
                    for route in routes:
                        self.logger(f"Checking route: {route['name']} (ID: {route['id']})")
                        self.logger(f"Route destination: {route['destination']}")

                        if route["destination"] == cidr:
                            next_hop = route["next_hop"]["address"]
                            self.logger(f"Found next hop {next_hop} for CIDR {cidr}")
                            return next_hop

            self.logger(f"No route found for CIDR {cidr}")
            return None

        except ApiException as e:
            self.logger(f"Error getting next hop: {e}")
            raise ApiException(f"Error getting next hop: {e}") from e

    def _find_next_hop_in_routes(self, routes: list, cidr: str) -> Optional[str]:
        """Find the next hop IP address in a list of routes for a given CIDR.

        Args:
            routes (list): List of route objects
            cidr (str): The CIDR to find a route for

        Returns:
            Optional[str]: The next hop IP address, or None if not found
        """
        # First try exact CIDR match
        for route in routes:
            if route["destination"] == cidr:
                next_hop = route["next_hop"]["address"]
                self.logger(f"Found exact match for CIDR {cidr} with next hop {next_hop}")
                return next_hop

        # If no exact match, find the smallest prefix that contains the CIDR
        target_network = ip_network(cidr)
        matching_routes = []

        for route in routes:
            try:
                route_network = ip_network(route["destination"])
                if target_network.subnet_of(route_network):
                    matching_routes.append((route, route_network.prefixlen))
            except ValueError:
                continue

        if matching_routes:
            # Sort by prefix length (smallest first) and get the first match
            matching_routes.sort(key=lambda x: x[1])
            best_match = matching_routes[0][0]
            next_hop = best_match["next_hop"]["address"]
            self.logger(f"Found prefix match for CIDR {cidr} in {best_match['destination']} with next hop {next_hop}")
            return next_hop

        # If no prefix match, look for default route
        for route in routes:
            if route["destination"] == "0.0.0.0/0":
                next_hop = route["next_hop"]["address"]
                self.logger(f"Using default route for CIDR {cidr} with next hop {next_hop}")
                return next_hop

        return None

    def get_next_hop_for_par(self, range_id: str, api_version: str = "2026-06-01",
                            maturity: Optional[str] = None, generation: str = "2") -> str:
        """Get the next hop IP address for a public address range.

        Args:
            range_id (str): The ID of the public address range
            api_version (str, optional): API version to use. Defaults to "2026-06-01".
            maturity (str, optional): API maturity level. Defaults to None (omitted for GA APIs).
            generation (str, optional): API generation. Defaults to "2".

        Returns:
            str: The next hop IP address for the route, or None if not found

        Raises:
            ApiException: If there is an error getting the next hop
        """
        self.logger(f"Getting next hop for public address range: {range_id}")

        try:
            # Get the public address range information
            range_info = self.get_public_address_range(range_id, api_version, maturity, generation)

            # Get the CIDR from the range info
            cidr = range_info.get('cidr')
            if not cidr:
                raise ApiException(f"No CIDR found for public address range {range_id}")

            # Get all routing tables
            maturity_param = f"&maturity={maturity}" if maturity else ""
            list_tables = self._make_api_request(
                "GET",
                f"/v1/vpcs/{self.vpc_id}/routing_tables?version={api_version}&generation={generation}{maturity_param}"
            )

            if not list_tables or "routing_tables" not in list_tables:
                raise ApiException(f"No routing tables found for VPC {self.vpc_id}")

            # Search through routing tables
            for table in list_tables["routing_tables"]:
                # Check if this is an internet ingress routing table
                if table.get("route_internet_ingress"):
                    self.logger(f"Found internet ingress routing table: {table['name']} (ID: {table['id']})")
                    table_id = table["id"]

                    # Get all routes in this table
                    routes = self._make_api_request(
                        "GET",
                        f"/v1/vpcs/{self.vpc_id}/routing_tables/{table_id}/routes?version={api_version}&generation={generation}{maturity_param}"
                    )["routes"]

                    # Try to find the next hop using helper method
                    next_hop = self._find_next_hop_in_routes(routes, cidr)
                    if next_hop:
                        return next_hop

            self.logger(f"No matching route found for CIDR {cidr}")
            return None

        except ApiException as e:
            self.logger(f"Error getting next hop for public address range: {e}")
            raise ApiException(f"Error getting next hop for public address range: {e}") from e

    def get_public_address_range(self, range_id, api_version="2026-06-01", maturity: Optional[str] = None, generation="2"):
        """Get information about a public address range.

        Args:
            range_id (str): The ID of the public address range to get
            api_version (str, optional): API version to use. Defaults to "2026-06-01".
            maturity (str, optional): API maturity level. Defaults to None (omitted for GA APIs).
            generation (str, optional): API generation. Defaults to "2".

        Returns:
            dict: The public address range information

        Raises:
            ApiException: If there is an error getting the range
        """
        self.logger("Getting public address range information")
        self.logger(f"Range ID: {range_id}")
        self.logger(f"VPC_URL: {self.vpc_url}")

        try:
            conn = http.client.HTTPSConnection(self.vpc_url.replace('https://', ''))
            headers = {
                'Authorization': self.get_token(),
                'Content-Type': 'application/json',
                'X-IBM-Cloud-API-Version': api_version,
                'X-IBM-Cloud-Generation': generation
            }
            # Public Address Range (PAR) GA endpoints do not accept maturity parameter
            if maturity and maturity != "ga":
                headers['X-IBM-Cloud-Maturity'] = maturity
                maturity_param = f"&maturity={maturity}"
            else:
                maturity_param = ""
            conn.request("GET", f"/v1/public_address_ranges/{range_id}?version={api_version}&generation={generation}{maturity_param}", headers=headers)
            response = conn.getresponse()
            if response.status != 200:
                raise ApiException(f"Failed to get public address range: {response.status} {response.reason}")

            range_info = json.loads(response.read().decode("utf-8"))
            self.logger(f"Range information: {range_info}")
            return range_info
        except Exception as e:
            raise ApiException(f"Unexpected error: {e}") from e

    def _update_range_zone(self, range_id, api_version, maturity, generation):
        """Update the zone of a public address range.

        Args:
            range_id: The ID of the public address range to update
            api_version: API version to use
            maturity: API maturity level
            generation: API generation

        Returns:
            dict: The updated public address range information

        Raises:
            ApiException: If there is an error updating the range
        """
        self.logger("Updating public address range target zone")
        conn = http.client.HTTPSConnection(self.vpc_url.replace('https://', ''))
        try:
            headers = {
                'Authorization': self.get_token(),
                'Content-Type': 'application/json',
                'X-IBM-Cloud-API-Version': api_version,
                'X-IBM-Cloud-Generation': generation
            }
            # public_address_ranges GA endpoints do not accept maturity parameter
            if maturity and maturity != "ga":
                headers['X-IBM-Cloud-Maturity'] = maturity
                maturity_param = f"&maturity={maturity}"
            else:
                maturity_param = ""
            range_patch_model = {
                "target": {
                    "zone": {
                        "name": self.vsi_local_az
                    }
                }
            }

            self.logger(f"Update range_patch_model: {range_patch_model}")
            conn.request("PATCH",
                        f"/v1/public_address_ranges/{range_id}?version={api_version}&generation={generation}{maturity_param}",
                        body=json.dumps(range_patch_model),
                        headers=headers)

            response = conn.getresponse()
            if response.status != 200:
                raise ApiException(f"Failed to update public address range: {response.status} {response.reason}")
            updated_range = json.loads(response.read().decode("utf-8"))
            self.logger(f"Update response: {updated_range}")
            self.logger("Successfully updated public address range")
            return updated_range
        finally:
            conn.close()

    def check_par_zone_compatibility(
        self,
        range_id: str,
        api_version: str = "2026-06-01",
        maturity: Optional[str] = None,
        generation: str = "2"
    ) -> Tuple[bool, str]:
        """Check if the public address range and VSI are in the same zone.

        Args:
            range_id: The ID of the public address range to check
            api_version: API version to use
            maturity: API maturity level
            generation: API generation

        Returns:
            A tuple containing:
                - bool: True if zones match, False otherwise
                - str: Current zone name

        Raises:
            ApiException: If there is an error checking zone compatibility
        """
        self.logger("Checking zone compatibility")
        try:
            range_info = self.get_public_address_range(range_id, api_version, maturity, generation)
            current_zone = range_info.get('target', {}).get('zone', {}).get('name')
            self.logger(f"Current range zone: {current_zone}")
            self.logger(f"VSI local zone: {self.vsi_local_az}")
            zones_match = current_zone == self.vsi_local_az
            return zones_match, current_zone
        except Exception as e:
            self.logger(f"Error checking zone compatibility: {e}")
            raise ApiException(f"Error checking zone compatibility: {e}") from e

    def update_public_address_range(self, range_id, api_version="2026-06-01",
                                  maturity: Optional[str] = None, generation="2"):
        """Update the target zone of a public address range to match the VSI's local availability zone.

        Args:
            range_id (str): The ID of the public address range to update
            api_version (str, optional): API version to use. Defaults to "2026-06-01".
            maturity (str, optional): API maturity level. Defaults to None (omitted for GA APIs).
            generation (str, optional): API generation. Defaults to "2".

        Returns:
            dict: The updated public address range information if updated, None if no update needed

        Raises:
            ApiException: If there is an error updating the range
        """
        self.logger("Checking public address range target zone")
        try:
            zones_match, _ = self.check_par_zone_compatibility(range_id, api_version,
                                                             maturity, generation)
            if not zones_match:
                return self._update_range_zone(range_id, api_version, maturity, generation)
            self.logger("No update needed - range already in correct zone")
            return None
        except ApiException as e:
            self.logger(f"Error updating public address range: {e}")
            raise ApiException(f"Error updating public address range: {e}") from e
        except Exception as e:
            self.logger(f"Unexpected error: {e}")
            raise ApiException(f"Unexpected error: {e}") from e

    def check_next_hop_in_internet_ingress(self, next_hop_ip: str) -> bool:
        """Check if a specific next hop IP is used in the internet ingress routing table.

        Args:
            next_hop_ip (str): The next hop IP address to check for

        Returns:
            bool: True if the next hop IP is found in the internet ingress routing table, False otherwise

        Raises:
            ApiException: If there is an error checking the routing tables
        """
        self.logger("Checking next hop in internet ingress routing table")
        self.logger(f"Next hop IP to check: {next_hop_ip}")
        self.logger(f"VPC ID: {self.vpc_id}")
        self.logger(f"VPC URL: {self.vpc_url}")

        try:
            # Get token for authentication
            token = self.get_token()

            # Set up HTTP connection
            vpc_host = self.vpc_url.replace('https://', '')
            conn = http.client.HTTPSConnection(vpc_host)

            # Set up headers
            headers = {
                'Authorization': token,
                'Content-Type': 'application/json',
                'X-IBM-Cloud-API-Version': self.API_VERSION,
                'X-IBM-Cloud-Generation': '2'
            }

            # Get all routing tables
            self.logger("Getting routing tables...")
            conn.request("GET", f"/v1/vpcs/{self.vpc_id}/routing_tables?version={self.API_VERSION}&generation=2",
                        headers=headers)
            response = conn.getresponse()

            if response.status != 200:
                raise ApiException(f"Failed to get routing tables: {response.status} {response.reason}")

            list_tables = json.loads(response.read().decode("utf-8"))

            if not list_tables or "routing_tables" not in list_tables:
                raise ApiException(f"No routing tables found for VPC {self.vpc_id}")

            # Search through routing tables
            for table in list_tables["routing_tables"]:
                # Check if this is an internet ingress routing table
                if table.get("route_internet_ingress"):
                    self.logger(f"Found internet ingress routing table: {table['name']} (ID: {table['id']})")
                    table_id = table["id"]

                    # Get all routes in this table
                    conn.request("GET",
                               f"/v1/vpcs/{self.vpc_id}/routing_tables/{table_id}/routes?version={self.API_VERSION}&generation=2",
                               headers=headers)
                    response = conn.getresponse()

                    if response.status != 200:
                        raise ApiException(f"Failed to get routes: {response.status} {response.reason}")

                    routes = json.loads(response.read().decode("utf-8"))["routes"]

                    # Check each route for the next hop IP
                    for route in routes:
                        self.logger(f"Checking route: {route['name']} (ID: {route['id']})")
                        self.logger(f"Route next hop: {route['next_hop']['address']}")

                        if route["next_hop"]["address"] == next_hop_ip:
                            self.logger(f"Found matching next hop {next_hop_ip} in route {route['id']}")
                            return True

            self.logger(f"Next hop {next_hop_ip} not found in any internet ingress routing table")
            return False

        except ApiException as e:
            self.logger(f"Error checking next hop in internet ingress routing table: {e}")
            raise ApiException(f"Error checking next hop in internet ingress routing table: {e}") from e
        except Exception as e:
            self.logger(f"Unexpected error: {e}")
            raise ApiException(f"Unexpected error: {e}") from e

def get_next_hop_for_par(range_id: str, vpc_url: str = "", api_key: str = "",
                        api_version: str = "2026-06-01", maturity: Optional[str] = None,
                        generation: str = "2") -> str:
    """Get the next hop IP address for a public address range.

    Args:
        range_id (str): The ID of the public address range
        vpc_url (str, optional): IBM Cloud VPC regional URL. Defaults to "".
        api_key (str, optional): IBM Cloud API key. Defaults to "".
        api_version (str, optional): API version to use. Defaults to "2026-06-01".
        maturity (str, optional): API maturity level. Defaults to None (omitted for GA APIs).
        generation (str, optional): API generation. Defaults to "2".

    Returns:
        str: The next hop IP address for the route, or None if not found

    Raises:
        ApiException: If there is an error getting the next hop
    """
    ha_fail_over = HAFailOver()
    ha_fail_over.vpc_url = vpc_url
    ha_fail_over.apikey = api_key

    # Get instance metadata to set VPC ID
    instance_metadata = ha_fail_over.get_instance_metadata()
    if "vpc" in instance_metadata:
        ha_fail_over.vpc_id = instance_metadata["vpc"]["id"]

    return ha_fail_over.get_next_hop_for_par(range_id, api_version, maturity, generation)

def fail_over_public_address_range(range_id, vpc_url="", api_key="", api_version="2026-06-01",
                                 maturity: Optional[str] = None, generation="2", nexthop_ip_1="", nexthop_ip_2=""):
    """Update the target zone of a public address range to match the VSI's local availability zone.

    Args:
        range_id (str): The ID of the public address range to update
        vpc_url (str, optional): IBM Cloud VPC regional URL. Defaults to "".
        api_key (str, optional): IBM Cloud API key. Defaults to "".
        api_version (str, optional): API version to use. Defaults to "2026-06-01".
        maturity (str, optional): API maturity level. Defaults to None (omitted for GA APIs).
        generation (str, optional): API generation. Defaults to "2".
        nexthop_ip_1 (str, optional): IP address of the first VSI. Defaults to "".
        nexthop_ip_2 (str, optional): IP address of the second VSI. Defaults to "".

    Returns:
        dict: The updated public address range information if updated, None if no update needed
    """
    ha_fail_over = HAFailOver()
    ha_fail_over.vpc_url = vpc_url
    ha_fail_over.apikey = api_key
    ha_fail_over.ext_ip_1 = nexthop_ip_1
    ha_fail_over.ext_ip_2 = nexthop_ip_2

    # Get instance metadata to set VSI local AZ and VPC ID
    instance_metadata = ha_fail_over.get_instance_metadata()
    if "zone" in instance_metadata:
        ha_fail_over.vsi_local_az = instance_metadata["zone"]["name"]
    if "vpc" in instance_metadata:
        ha_fail_over.vpc_id = instance_metadata["vpc"]["id"]
    else:
        raise ApiException("Could not get VPC ID from instance metadata")

    # Verify we have all required information
    if not ha_fail_over.vpc_id:
        raise ApiException("VPC ID is required but not set")
    if not ha_fail_over.vpc_url:
        raise ApiException("VPC URL is required but not set")

    next_hop = ha_fail_over.update_vpc_routing_table_route("SET", ["route_internet_ingress"])
    if next_hop:
        return ha_fail_over.update_public_address_range(range_id, api_version, maturity, generation)
    return None

def fail_over_check_par_zone_compatibility(
    range_id: str,
    vpc_url: str = "",
    api_key: str = "",
    api_version: str = "2026-06-01",
    maturity: str = "ga",
    generation: str = "2"
) -> Tuple[bool, str]:
    """Check if the public address range and VSI are in the same zone.

    Args:
        range_id: The ID of the public address range to check
        vpc_url: IBM Cloud VPC regional URL
        api_key: IBM Cloud API key
        api_version: API version to use
        maturity: API maturity level
        generation: API generation

    Returns:
        A tuple containing:
            - bool: True if zones match, False otherwise
            - str: Current zone name
    """
    ha_fail_over = HAFailOver()
    ha_fail_over.vpc_url = vpc_url
    ha_fail_over.apikey = api_key

    instance_metadata = ha_fail_over.get_instance_metadata()
    if "zone" in instance_metadata:
        ha_fail_over.vsi_local_az = instance_metadata["zone"]["name"]

    return ha_fail_over.check_par_zone_compatibility(
        range_id=range_id,
        api_version=api_version,
        maturity=maturity,
        generation=generation
    )

def fail_over(cmd):
    """_summary_

    Args:
        cmd (_type_): GET or SET

    Returns:
        _type_: _description_
    """
    ha_fail_over = HAFailOver()
    # self.logger("Request received from: " + remote_addr)
    made_update = ha_fail_over.update_vpc_routing_table_route(cmd)
    return "Updated Custom Route: " + str(made_update)


def fail_over_fip(cmd, vni_id, fip_id):
    """_summary_

    Args:
        cmd (_type_): add or remove
        vni_id (_type_): vni uuid
        fip_id (_type_): fip uuid
    """
    ha_fail_over = HAFailOver()
    ha_fail_over.update_vpc_fip(cmd, vni_id, fip_id)

def fail_over_floating_ip_stop(vpc_url, vni_id_1, vni_id_2, fip_id, api_key=""):
    """Stop floating IP failover.

    Args:
        vpc_url: IBM Cloud VPC regional URL
        vni_id_1: First VNI ID
        vni_id_2: Second VNI ID
        fip_id: Floating IP ID
        api_key: IBM Cloud API key
    """
    ha_fail_over = HAFailOver()
    ha_fail_over.vpc_url = vpc_url
    ha_fail_over.apikey = api_key
    vni_metadata = ha_fail_over.get_vni_metadata()
    if "virtual_network_interfaces" in vni_metadata:
        for vni in vni_metadata["virtual_network_interfaces"]:
            if vni["id"] == vni_id_1 or vni["id"] == vni_id_2:
                local_vni_id = vni["id"]
                ha_fail_over.update_vpc_fip("remove", local_vni_id, fip_id)
    fip_id, fip_ip = fail_over_get_attached_fip(api_key)
    return fip_id, fip_ip


def fail_over_floating_ip_start(vpc_url, vni_id_1, vni_id_2, fip_id, api_key=""):
    ha_fail_over = HAFailOver()
    ha_fail_over.vpc_url = vpc_url
    ha_fail_over.apikey = api_key

    # Get VNI metadata directly since we don't use instance metadata
    vni_metadata = ha_fail_over.get_vni_metadata()
    if "virtual_network_interfaces" in vni_metadata:
        for vni in vni_metadata["virtual_network_interfaces"]:
            if vni["id"] == vni_id_1 or vni["id"] == vni_id_2:
                local_vni_id = vni["id"]
                if local_vni_id == vni_id_1:
                    remote_vni_id = vni_id_2
                else:
                    remote_vni_id = vni_id_1
                ha_fail_over.update_vpc_fip("remove", remote_vni_id, fip_id)
                ha_fail_over.update_vpc_fip("add", local_vni_id, fip_id)
    fip_id, fip_ip = fail_over_get_attached_fip(api_key)
    return fip_id, fip_ip

def fail_over_get_attached_fip(api_key):
    ha_fail_over = HAFailOver()
    ha_fail_over.apikey = api_key
    instance_metadata = ha_fail_over.get_instance_interface_metadata()
    for net_i in instance_metadata["network_interfaces"]:
        for floating_ips in net_i["floating_ips"]:
            attached_fip_id = floating_ips["id"]
            attached_fip_ip = floating_ips["address"]
            return attached_fip_id, attached_fip_ip
    return None , None

def fail_over_cr_vip (cmd , vpc_url, ext_ip_1 , ext_ip_2, api_key=""):
    """_summary_

    Args:
        cmd (string): SET or GET
        vpc_url (string): IBM cloud regional VPC URL
        ext_ip_1 (string): Ip of the first VSI
        ext_ip_2 (string): Ip of the second VSI
        apy_key  (string)
    Returns:
        _type_: _description_
    """
    ha_fail_over = HAFailOver()
    ha_fail_over.vpc_url = vpc_url
    ha_fail_over.ext_ip_2 = ext_ip_2
    ha_fail_over.ext_ip_1 = ext_ip_1
    ha_fail_over.apikey = api_key
    instance_metadata = ha_fail_over.get_instance_metadata()
    if "vpc" in instance_metadata:
        ha_fail_over.vpc_id = instance_metadata["vpc"]["id"]
    if "zone" in instance_metadata:
        ha_fail_over.vsi_local_az = instance_metadata["zone"]["name"]

    next_hop = ha_fail_over.update_vpc_routing_table_route(cmd)
    return next_hop


def usage_fip():
    """_summary_
    """
    print("{0} [FIP] [CMD add|remove] [VNI_ID] [FIP_ID]"f'{sys.argv[0]}')
    print("\n")

def fail_over_check_next_hop_internet_ingress(next_hop_ip: str, vpc_url: str = "", api_key: str = "") -> bool:
    """Check if a specific next hop IP is used in the internet ingress routing table.

    Args:
        next_hop_ip (str): The next hop IP address to check for
        vpc_url (str, optional): IBM Cloud VPC regional URL. Defaults to "".
        api_key (str, optional): IBM Cloud API key. Defaults to "".

    Returns:
        bool: True if the next hop IP is found in the internet ingress routing table, False otherwise

    Raises:
        ApiException: If there is an error checking the routing tables
    """
    ha_fail_over = HAFailOver()
    ha_fail_over.vpc_url = vpc_url
    ha_fail_over.apikey = api_key

    # Get instance metadata to set VPC ID
    instance_metadata = ha_fail_over.get_instance_metadata()
    if "vpc" in instance_metadata:
        ha_fail_over.vpc_id = instance_metadata["vpc"]["id"]

    return ha_fail_over.check_next_hop_in_internet_ingress(next_hop_ip)

if __name__ == "__main__":
    if len(sys.argv) > 2:
        if sys.argv[1] == "FIP":
            fail_over_fip(sys.argv[2], sys.argv[3], sys.argv[4])
        elif sys.argv[1] == "ROUTE":
            fail_over(sys.argv[2])
    else:
        print(
            "Error must provide parameter usage: ibm_cloud_pacemaker_fail_over.py ROUTE GET|SET"
        )
        usage_fip()
