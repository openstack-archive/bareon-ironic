=========================================
Bareon-based deployment driver for Ironic
=========================================

``bareon_ironic`` package adds support for Bareon to OpenStack Ironic.
Ironic [#]_ is baremetal provisioning service with support of multiple hardware
types. Ironic architecture is able to work with deploy agents. Deploy agent
is a service that does provisioning tasks on the node side. Deploy agent is
integrated into bootstrap ramdisk image.
``bareon_ironic`` contains pluggable drivers code for Ironic that uses
Bareon [#]_ as deploy agent. Current implementation requires and tested with
Ironic/Nova Stable Kilo release.

Features overview
=================

Flexible deployment configuration
---------------------------------
A JSON called deploy_config carries partitions schema, partitioning behavior,
images schema, various deployment args. It can be passed through various
places like nova VM metadata, image metadata, node metadata etc. Resulting
JSON is a result of merge operation, that works basing on the priorities
configureed in ``/etc/ironic/ironic.conf``.

LVM support
-----------
Configuration JSON allows to define schemas with mixed partitions and logical
volumes.

Multiple partitioning behaviors available
-----------------------------------------

- Verify. Reads schema from the baremetal hardware and compares with user
  schema.
- Verify+clean. Compares baremetal schema with user schema, wipes particular
  filesystems basing on the user schema.
- Clean. Wipes disk, deploys from scratch.

Multiple image deployment
-------------------------

Configuration JSON allows to define more than 1 image. The Bareon Ironic driver
provides handles to switch between deployed images. This allows to perform a
baremetal node upgrades with minimal downtime.

Block-level copy & file-level Image deployment
----------------------------------------------

Bareon Ironic driver allows to do both: bare_swift drivers for block-level and
bare_rsync drivers for file-level.

Deployment termination
----------------------

The driver allows to teminate deployment in both silent (wait-callback) and
active (deploying) phases.

Post-deployment hooks
---------------------

Two hook mechanisms available: on_fail_script and deploy actions. The first one
is a user-provided shell script which is executed inside the deploy ramdisk if
deployment has failed. The latter is a JSON-based, allows to define various
actions with associated resources and run them after the deployment has passed.


Building HTML docs
==================

$ pip install sphinx
$ cd bareon-ironic/doc && make html


.. [#] https://wiki.openstack.org/wiki/Ironic
.. [#] https://wiki.openstack.org/wiki/Bareon
