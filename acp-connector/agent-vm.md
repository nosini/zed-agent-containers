# On-demand build/test VM

Agents connect using `ssh -F ~/.ssh/agent-vm.conf agent-vm`. The desktop
starts the configured VM if it is off, waits for its SSH identification,
and forwards the connection. TCP connections that close or stall before
SSH is ready are retried within the configured boot limit, keeping the
original client connection open. A cold boot can take several minutes.
After the last relayed SSH connection closes, the VM shuts down gracefully
after 15 minutes, unless a job has a keep-awake hold. VMs started manually
stay under manual control. The helper never forces a power-off.

## Install

Create a persistent VM in Virtual Machine Manager, disable its autostart,
and give it a reserved/static IPv4 address. Set up the guest firewall and
VLAN first; this helper does not change networking. The desktop must be
able to reach the guest's SSH port. Any encrypted backing disk must be
unlocked and mounted before the VM starts, and encrypted guests need
unattended unlocking, such as a virtual TPM.

The shared agent home needs an existing `~/.ssh/agent-vm.conf` containing
only `Host agent-vm`, its guest address/account, dedicated identity key,
`IdentitiesOnly yes`, `IdentityAgent none`, `ForwardAgent no`,
`BatchMode yes`, `StrictHostKeyChecking yes` and a pinned guest
`UserKnownHostsFile`. Verify direct SSH access before enabling activation.
Do not reuse a personal signing key or put private keys in this repository.

The desktop needs Python 3 with the distribution's libvirt bindings.
Use the package matching `/usr/bin/python3`; openSUSE uses names such as
`python313-libvirt-python` for Python 3.13. Virtual Machine Manager
commonly installs the bindings already.

From a host terminal in this repository, substitute the existing VM name
and guest IPv4 address:

```sh
sudo python3 acp-connector/install-agent-vm.py \
  --domain agent-test --address 192.0.2.2 --socket-user "$(id -un)"
./install.sh --agent-vm
```

The first command installs a root-owned configuration fixing one domain
UUID and one SSH endpoint. It enables only a Unix socket unit. The second
rebuilds both images, installs and tests the connectors, mounts the
dedicated socket directory and read-only client into new containers and
adds `ProxyCommand` and a longer connection timeout to the existing VM
SSH profile. Authentication settings and host keys are preserved. The
socket files are accessible only to the specified desktop account; the
directory is root-owned and mounted read-only in containers.

VM profile setup runs before installed broker files or settings change.
If setup fails, fix the reported problem and rerun the command; the
previous connector installation remains active. The old
`--vm-socket-dir /run/acp-agent-vm` installer spelling is still accepted.
The SSH timeout includes the configured boot limit, time to finish a
shutdown already underway, and a startup margin.

The connector installer preserves edited environment instructions. If it
reports that it kept yours, merge the updated `Build and test VM` section
from `agent-environment.md`, including the keep-awake commands, before
starting detached work. If you use the supplied instructions unchanged,
you can install the current template with:

```sh
install -m 644 acp-connector/agent-environment.md \
  ~/.config/acp-connector/agent-environment.md
```

Close and reopen agent connections to pick up the mounts and rebuilt
images. To test the cold start, first shut the VM down normally in Virtual
Machine Manager after finishing all work, then have an agent run:

```sh
ssh -F ~/.ssh/agent-vm.conf agent-vm 'hostname; sudo -n true'
```

Keep the VM's DHCP reservation and the SSH profile aligned with the helper
address. A moved/recreated guest must still pass the existing host-key pin.
The container images do not need additional packages for this feature.

## Keep detached jobs awake

Open relayed SSH connections keep the VM awake automatically. The helper
cannot see processes inside the guest, console sessions or direct SSH
connections. Acquire a hold **before** starting detached builds, reboot
tests or anything that must survive an SSH disconnect:

```sh
token=$(python3 -I /opt/agent-vm-client.py hold project/build)
ssh -F ~/.ssh/agent-vm.conf agent-vm 'your-background-job-command'
# After checking that the remote work has finished:
python3 -I /opt/agent-vm-client.py release "$token"
```

Record the token in the project's task notes when work spans sessions.
Holds remain until explicitly released, including after container exits
or helper restarts. Forgotten holds keep the VM on. Inspect them with:

```sh
python3 -I /opt/agent-vm-client.py status
```

For a foreground command that completes all its remote work before
returning, `run` acquires a hold and releases it only on success:

```sh
python3 -I /opt/agent-vm-client.py run project/test -- \
  ssh -F ~/.ssh/agent-vm.conf agent-vm 'your-test-command'
```

On interruption or failure, the hold remains so a remote job is protected.
Check that job before releasing its token. Other agents share the VM and
holds; do not release another task's hold.

## Maintenance and troubleshooting

Host logs are in `sudo journalctl -u acp-agent-vm.service`. Socket status is
`sudo systemctl status acp-agent-vm.socket`. A start failure commonly means
the backing disk is unavailable, the guest is waiting for an unlock, or
the guest address/firewall no longer matches the setup. Host-key failures
must be investigated without disabling verification.

The helper remains as a small host process after its first activation;
the guest's CPU and RAM are released when the VM shuts down. If graceful
shutdown fails or takes more than two minutes, it leaves the VM under
manual control and logs the failure. It does not retry the shutdown.
Pausing or suspending a managed VM also returns it to manual control,
without continuing periodic hypervisor checks. Restarting the helper
leaves any already-running VM under manual control, because it cannot
prove who started that instance. Shut the guest down normally once to return to
automatic management.

If the activation directory or client is unavailable when a new agent
container starts, the broker skips those mounts and keeps ordinary agent
work available. Once the helper is restored, reconnect the agent to get
VM access; existing containers do not acquire mounts dynamically.

To update the root helper, finish/check jobs and release their holds,
close relayed SSH sessions, then run:

```sh
sudo systemctl stop acp-agent-vm.socket acp-agent-vm.service
# Rerun the install-agent-vm.py command with your configuration.
```

This stops the relay without powering off the guest. Reinstallation
refuses while the service is active or durable holds remain. Connector
updates alone do not restart the VM helper.

The same client can inspect or release holds from a desktop terminal:

```sh
python3 acp-connector/agent-vm-client.py --socket-dir /run/acp-agent-vm status
# After verifying that the named job is finished:
python3 acp-connector/agent-vm-client.py --socket-dir /run/acp-agent-vm release TOKEN
```

To change the idle period, use `--idle-minutes` when reinstalling. To remove
activation, remove `--vm-socket-dir /run/acp-agent-vm` from `broker.env`,
remove the profile's `ProxyCommand` and `ConnectTimeout`, disable the
system socket/service, and reconnect agents. SSH then uses the original
direct transport.
