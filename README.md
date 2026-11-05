# IBM Cloud Fail Over Functions

## Overview

IBM Cloud Fail Over is a Python module designed to automate failover processes for applications hosted on IBM Cloud. It is the core library used by the following Pacemaker OCF resource agents merged into [ClusterLabs/resource-agents](https://github.com/ClusterLabs/resource-agents):

- [`ibm-cloud-vpc-cr-vip`](https://github.com/ClusterLabs/resource-agents/blob/main/heartbeat/ibm-cloud-vpc-cr-vip.in) — Custom Route VIP failover (same AZ and cross-AZ)
- [`ibm-cloud-vpc-move-fip`](https://github.com/ClusterLabs/resource-agents/blob/main/heartbeat/ibm-cloud-vpc-move-fip.in) — Floating IP failover (single AZ)
- [`ibm-cloud-vpc-move-par`](https://github.com/ClusterLabs/resource-agents/blob/main/heartbeat/ibm-cloud-vpc-move-par.in) — Public Address Range (PAR) failover

The module provides essential functions to manage IBM Cloud VPC failover scenarios effectively, ensuring high availability and reliability.

## Features

- **Seamless IBM Cloud Integration**: Natively integrates with IBM Cloud VPC APIs — no additional middleware required.
- **Pacemaker Resource Management**: Serves as the backing library for Pacemaker OCF resource agents, enabling IBM Cloud VPC resources to be managed directly from a Pacemaker cluster.
- **High Availability for IBM Cloud Applications**: Automates failover to ensure continuous availability of applications running on IBM Cloud VPC.
- **Supported Failover Modes**:
  - **Custom Route VIP — Active/Passive Same AZ**: Moves a VPC custom route next-hop to the active VSI within the same Availability Zone.
  - **Custom Route VIP — Cross/Multi-AZ**: Moves a VPC custom route next-hop across Availability Zones to the active VSI.
  - **Floating IP Failover (Single AZ)**: Moves a Floating IP between VNIs within a single Availability Zone.
  - **Public Address Range (PAR) Failover**: Updates the target zone of a Public Address Range to match the active VSI's Availability Zone.
- **Easy Configuration and Deployment**: Minimal parameters per function; designed to drop into existing Pacemaker OCF resource agent scripts.
- **Trusted Profile IAM or API Key Authentication**: Supports keyless authentication via IBM Cloud Instance Metadata Trusted Profiles, with optional fallback to an explicit IBM Cloud API key.

## Prerequisites

- A working IBM Cloud account with access to the VPC infrastructure service.
- A VNI-based [Virtual Network Interface](https://cloud.ibm.com/docs/vpc?topic=vpc-vni-about) pair to be used as the active/passive endpoints.
- `allow_ip_spoofing` enabled on each Virtual Network Interface in the active/passive pair.
- [Instance Metadata Service enabled](https://cloud.ibm.com/docs/vpc?topic=vpc-imd-configure-service&interface=ui) on both VSIs in the active/passive pair.
- Pacemaker installed and configured on the active/passive cluster nodes.
- IAM authentication: either a [Trusted Profile](https://cloud.ibm.com/docs/vpc?topic=vpc-imd-trusted-profile-metadata&interface=ui) linked to the VSI (recommended, no key stored on host) or an [IBM Cloud API key](https://cloud.ibm.com/docs/account?topic=account-userapikey&interface=ui).

## Installation

You can install the IBM Cloud Fail Over module using pip:

```bash
pip install ibm_cloud_fail_over
```

## Usage

### Main Functions

#### `fail_over_cr_vip`

This function manages the failover of a Cloud Resource Virtual IP (CR VIP).

**Usage:**

```python
from ibm_cloud_fail_over import fail_over_cr_vip

# Example usage
active_ip = fail_over_cr_vip(
    cmd='SET',
    vpc_url='IBM Cloud VPC regional URL',
    ext_ip_1='IP of the first VSI',
    ext_ip_2='IP of the second VSI',
    api_key='Optional: API key',
    interface_name='eth0',                  # Optional: network interface
    vip_addresses='10.240.0.10,10.240.0.11' # Optional: VIP addresses
)
print(active_ip)
```

**Parameters:**

| Parameter        | Type | Description                                                                                                                |
|------------------|------|----------------------------------------------------------------------------------------------------------------------------|
| `cmd`            | str  | Command to execute: 'SET' to update routes and assign VIPs, 'GET' to retrieve current next hop, or 'UNSET' to remove VIPs. |
| `vpc_url`        | str  | IBM Cloud regional VPC URL.                                                                                                |
| `ext_ip_1`       | str  | IP address of the first VSI.                                                                                               |
| `ext_ip_2`       | str  | IP address of the second VSI.                                                                                              |
| `api_key`        | str  | Optional: IBM Cloud API key (defaults to "").                                                                              |
| `interface_name` | str  | Optional: Local network interface to assign vip_addresses to (defaults to "").                                             |
| `vip_addresses`  | str  | Optional: Comma-separated VIP addresses to assign locally (defaults to "").                                                |

**Returns:**

- `str`: Next hop IP address.

#### `fail_over_floating_ip_start`

This function manages the failover of a Floating IP by attaching it to the caller endpoint (VSI/BM).

**Usage:**

```python
from ibm_cloud_fail_over import fail_over_floating_ip_start

# Example usage
fip_id, fip_ip = fail_over_floating_ip_start(
    vpc_url='IBM Cloud VPC regional URL',
    vni_id_1='First VNI ID',
    vni_id_2='Second VNI ID',
    fip_id='the floating IP UUID',
    api_key='Optional: API key'
)
print(fip_id, fip_ip)
```

**Parameters:**

| Parameter  | Type      | Description                                                                    |
|------------|-----------|--------------------------------------------------------------------------------|
| `vpc_url`  | str (url) | IBM Cloud VPC regional URL.                                                    |
| `vni_id_1` | str (uuid)| First VNI ID.                                                                  |
| `vni_id_2` | str (uuid)| Second VNI ID.                                                                 |
| `fip_id`   | str (uuid)| Floating IP UUID to move.                                                      |
| `api_key`  | str       | Optional: IBM Cloud API access key (needed only if not using trusted profile). |

**Returns:**

- A tuple containing:
  - `str`: Floating IP ID, or None if not found
  - `str`: Floating IP address, or None if not found

#### `fail_over_floating_ip_stop`

This function manages the failover of a Floating IP by detaching it from the caller endpoint (VSI/BM).

**Usage:**

```python
from ibm_cloud_fail_over import fail_over_floating_ip_stop

# Example usage
fip_id, fip_ip = fail_over_floating_ip_stop(
    vpc_url='IBM Cloud VPC regional URL',
    vni_id_1='First VNI ID',
    vni_id_2='Second VNI ID',
    fip_id='the floating IP UUID',
    api_key='Optional: API key'
)
print(fip_id, fip_ip)
```

**Parameters:**

| Parameter  | Type      | Description                                                                    |
|------------|-----------|--------------------------------------------------------------------------------|
| `vpc_url`  | str (url) | IBM Cloud VPC regional URL.                                                    |
| `vni_id_1` | str (uuid)| First VNI ID.                                                                  |
| `vni_id_2` | str (uuid)| Second VNI ID.                                                                 |
| `fip_id`   | str (uuid)| Floating IP UUID to move.                                                      |
| `api_key`  | str       | Optional: IBM Cloud API access key (needed only if not using trusted profile). |

**Returns:**

- A tuple containing:
  - `str`: Floating IP ID, or None if not found
  - `str`: Floating IP address, or None if not found

#### `fail_over_get_attached_fip`

This function retrieves the attached Floating IP for the caller endpoint (VSI/BM).

**Usage:**

```python
from ibm_cloud_fail_over import fail_over_get_attached_fip

# Example usage
attached_fip_id, attached_fip_ip = fail_over_get_attached_fip(api_key='Optional: API key')

print(attached_fip_ip)
print(attached_fip_id)
```

**Parameters:**

| Parameter | Type | Description                                                          |
|-----------|------|----------------------------------------------------------------------|
| `api_key` | str  | IBM Cloud API access key (needed only if not using trusted profile). |

**Returns:**

- A tuple containing:
  - `attached_fip_id`: The UUID of the attached Floating IP, or None if not found
  - `attached_fip_ip`: The IP address of the attached Floating IP, or None if not found

#### `fail_over_public_address_range`

This function manages the failover of a public address range by updating its target zone to match the VSI's local availability zone.

**Usage:**

```python
from ibm_cloud_fail_over import fail_over_public_address_range

# Example usage
result = fail_over_public_address_range(
    range_id='public_address_range_uuid',
    vpc_url='IBM Cloud VPC regional URL',
    api_key='Optional: API key',
    api_version='2026-06-01',                   # Optional: API version
    maturity=None,                              # Optional: API maturity level (None for GA APIs)
    generation='2',                             # Optional: API generation
    nexthop_ip_1='IP address of the first VSI', # Optional
    nexthop_ip_2='IP address of the second VSI' # Optional
)
print(result)
```

**Parameters:**

| Parameter      | Type      | Description                                                                    |
|----------------|-----------|--------------------------------------------------------------------------------|
| `range_id`     | str (uuid)| The ID of the public address range to update.                                  |
| `vpc_url`      | str (url) | IBM Cloud VPC regional URL.                                                    |
| `api_key`      | str       | Optional: IBM Cloud API access key (needed only if not using trusted profile). |
| `api_version`  | str       | Optional: API version (defaults to "2026-06-01").                              |
| `maturity`     | str       | Optional: API maturity level (defaults to None, omitted for GA APIs).          |
| `generation`   | str       | Optional: API generation (defaults to "2").                                    |
| `nexthop_ip_1` | str       | Optional: IP address of the first VSI (defaults to "").                        |
| `nexthop_ip_2` | str       | Optional: IP address of the second VSI (defaults to "").                       |

**Returns:**

- `Optional[dict]`: The updated public address range information if an update was needed, or None if no update was needed (range already in correct zone)

**Note:**

- The function automatically detects the VSI's local availability zone
- The range is only updated if its current zone differs from the VSI's local zone
- Requires the same instance metadata service and trusted profile setup as other functions

#### `fail_over_check_par_zone_compatibility`

This function checks if a public address range and the VSI are in the same availability zone.

**Usage:**

```python
from ibm_cloud_fail_over import fail_over_check_par_zone_compatibility

# Example usage
zones_match, current_zone = fail_over_check_par_zone_compatibility(
    range_id='public_address_range_uuid',
    vpc_url='IBM Cloud VPC regional URL',
    api_key='Optional: API key',
    api_version='2026-06-01', # Optional: API version
    maturity='ga',            # Optional: API maturity level
    generation='2'            # Optional: API generation
)

if zones_match:
    print(f'PAR and VSI are in the same zone: {current_zone}')
else:
    print(f'PAR and VSI are in different zones. PAR zone: {current_zone}')
```

**Parameters:**

| Parameter     | Type      | Description                                                                    |
|---------------|-----------|--------------------------------------------------------------------------------|
| `range_id`    | str (uuid)| The ID of the public address range to check.                                   |
| `vpc_url`     | str (url) | IBM Cloud VPC regional URL.                                                    |
| `api_key`     | str       | Optional: IBM Cloud API access key (needed only if not using trusted profile). |
| `api_version` | str       | Optional: API version (defaults to "2026-06-01").                              |
| `maturity`    | str       | Optional: API maturity level (defaults to "ga").                               |
| `generation`  | str       | Optional: API generation (defaults to "2").                                    |

**Returns:**

- A tuple containing:
  - `bool`: True if the public address range and VSI are in the same zone, False otherwise
  - `str`: The current zone name of the public address range

**Note:**

- The function automatically detects the VSI's local availability zone
- Requires the same instance metadata service and trusted profile setup as other functions
- Useful for checking zone compatibility before performing failover operations

#### `get_next_hop_for_par`

This function retrieves the next hop IP address for a public address range by looking up its CIDR in the internet ingress routing table.

**Usage:**

```python
from ibm_cloud_fail_over import get_next_hop_for_par

# Example usage
next_hop = get_next_hop_for_par(
    range_id='public_address_range_uuid',
    vpc_url='IBM Cloud VPC regional URL',
    api_key='Optional: API key',
    api_version='2026-06-01', # Optional: API version
    maturity=None,            # Optional: API maturity level (None for GA APIs)
    generation='2'            # Optional: API generation
)
print(next_hop)
```

**Parameters:**

| Parameter     | Type      | Description                                                           |
|---------------|-----------|-----------------------------------------------------------------------|
| `range_id`    | str (uuid)| The ID of the public address range.                                   |
| `vpc_url`     | str (url) | IBM Cloud VPC regional URL (defaults to "").                          |
| `api_key`     | str       | Optional: IBM Cloud API access key (defaults to "").                  |
| `api_version` | str       | Optional: API version (defaults to "2026-06-01").                     |
| `maturity`    | str       | Optional: API maturity level (defaults to None, omitted for GA APIs). |
| `generation`  | str       | Optional: API generation (defaults to "2").                           |

**Returns:**

- `Optional[str]`: The next hop IP address for the route, or None if not found

**Note:**

- The function automatically retrieves the public address range's CIDR and searches for it in the internet ingress routing table
- Uses prefix matching if no exact CIDR match is found, falling back to the default route (0.0.0.0/0) if needed
- Requires the same instance metadata service and trusted profile setup as other functions

#### `fail_over_check_next_hop_internet_ingress`

This function checks if a specific next hop IP address is used in the internet ingress routing table.

**Usage:**

```python
from ibm_cloud_fail_over import fail_over_check_next_hop_internet_ingress

# Example usage
is_used = fail_over_check_next_hop_internet_ingress(
    next_hop_ip='10.240.0.5',
    vpc_url='IBM Cloud VPC regional URL',
    api_key='Optional: API key'
)

if is_used:
    print(f'Next hop {next_hop_ip} is used in internet ingress routing table')
else:
    print(f'Next hop {next_hop_ip} is not found in internet ingress routing table')
```

**Parameters:**

| Parameter     | Type | Description                                          |
|---------------|------|------------------------------------------------------|
| `next_hop_ip` | str  | The next hop IP address to check for.                |
| `vpc_url`     | str  | IBM Cloud VPC regional URL (defaults to "").         |
| `api_key`     | str  | Optional: IBM Cloud API access key (defaults to ""). |

**Returns:**

- `bool`: True if the next hop IP is found in the internet ingress routing table, False otherwise

**Note:**

- Useful for verifying routing configuration before performing failover operations
- Only checks the internet ingress routing table
- Requires the same instance metadata service and trusted profile setup as other functions

## Troubleshooting

If you encounter issues while using the module, consider the following:

- **Invalid IP Addresses**: Ensure that the IP addresses provided for primary and secondary resources are valid and reachable.
- **Network Issues**: Check your network connectivity to the IBM Cloud resources.
- **Permissions**: Ensure that your IBM Cloud account has the necessary permissions to manage the specified resources.
- **Required Dependencies**: Enable [Instance Metadata Service](https://cloud.ibm.com/docs/vpc?topic=vpc-imd-configure-service&interface=ui) on both endpoints.
- **Optional Dependencies**: Enable [Trusted Profile](https://cloud.ibm.com/docs/vpc?topic=vpc-imd-trusted-profile-metadata&interface=ui) to avoid using an API access key.

For further assistance, please check the [GitHub Issues](https://github.com/IBM/ibm_cloud_fail_over/issues) page or contact the project maintainer.

## Contributing

We welcome contributions! To contribute to the project, please follow these steps:

1. Fork the repository.
2. Create a new branch (`git checkout -b feature/YourFeature`).
3. Make your changes and commit them (`git commit -m 'Add some feature'`).
4. Push to the branch (`git push origin feature/YourFeature`).
5. Create a new Pull Request.

## License

This project is licensed under the Apache License 2.0 - see the [LICENSE](LICENSE) file for details.

## Contact

For questions or support, please open an issue on GitHub or contact the project maintainer at [dev@gampel.net](mailto:dev@gampel.net).
