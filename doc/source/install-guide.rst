Installation Guide
==================

This guide describes installation on top of OpenStack Kilo release.
The ``bare_swift_ipmi`` driver used as example.

1. Install bareon_ironic package

2. Patch Nova service with bareon patch ``(patches/patch-nova-stable-kilo)``

3. Restart nova compute-service

4. Patch Ironic service with bareon patch ``(patches/patch-ironic-stable-kilo)``

5. Enable the driver: add ``bare_swift_ipmi`` to the list of ``enabled_drivers``
   in ``[DEFAULT]`` section of ``/etc/ironic/ironic.conf``.

6. Update ironic.conf using bareon sample ``(bareon_ironic/etc/ironic/ironic.conf)``

7. Restart ``ironic-api`` and ``ironic-conductor`` services

8. Build a Bareon ramdisk:

    8.1 Get Bareon source code ([1]_)

    8.2 Run build

    .. code-block:: console

        $ cd bareon && bash bareon/tests_functional/image_build/centos_minimal.sh

    Resulting images and SSH private key needed to access it will appear
    at /tmp/rft_image_build/.

9. Upload kernel and initrd images to the Glance image service.

10. Create a node in Ironic with ``bare_swift_ipmi`` driver and associate port with the node

    .. code-block:: console

        $ ironic node-create -d bare_swift_ipmi
        $ ironic port-create -n <node uuid> -a <MAC address>

11. Set IPMI address and credentials as described in the Ironic documentation [2]_.

12. Setup nova flavor as described in the Ironic documentation [2]_.

13. Set Bareon related driver's parameters for the node

    .. code-block:: console

        $ KERNEL=<UUID_of_kernel_image_in_Glance>
        $ INITRD=<UUID_of_initrd_image_in_Glance>
        $ PRIVATE_KEY_PATH=/tmp/rft_image_build/fuel_key
        $ ironic node-update <node uuid> add driver_info/deploy_kernel=$KERNEL \
        driver_info/deploy_ramdisk=$INITRD \
        driver_info/bareon_key_filename=$PRIVATE_KEY_PATH

14. Issue ironic validate command to check for errors

    .. code-block:: console

        $ ironic node-validate <node uuid>

After steps above the node is ready for deploying. User can invoke
``nova boot`` command and link appropriate deploy_config as described in User
Guide

.. [1] https://github.com/openstack/bareon
.. [2] http://docs.openstack.org/developer/ironic/deploy/install-guide.html
