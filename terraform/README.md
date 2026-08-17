# Terraform — dashboard plus four t3 sizes

One `terraform apply` brings up the whole thing: a dashboard server and
`t3.micro`, `t3.small`, `t3.medium`, `t3.large`, each running the same agent and
reporting in. Nothing to configure on the instances by hand.

## What you need

- An EC2 key pair in the target region.
- AWS credentials with EC2 permissions (`AmazonEC2FullAccess` covers it).
  Terraform reads them the usual ways — `AWS_PROFILE`, env vars, or
  `~/.aws/credentials`.

That is all. The agents find the server over the VPC's private network, so
there is no address to write down.

## Run it

```bash
aws configure                     # if you have not already
cd terraform

# -4 matters: many ISPs answer with an IPv6 address by default, and the
# security group rules are IPv4.
cat > terraform.tfvars <<EOF
key_name = "my-keypair"
ssh_cidr = "$(curl -4 -s ifconfig.me)/32"
EOF

cat terraform.tfvars              # sanity-check: dots, not colons
terraform init
terraform plan                    # read it: this creates five billable instances
terraform apply
```

Then open the `dashboard_url` from the output. Two to three minutes for package
install, a 60-second idle calibration per agent, and the first pushes. The
**Instance** toggle appears once two agents report.

Press **Stress all** to load the fleet and watch the smaller sizes separate from
the larger ones.

## Costs

Five on-demand instances bill by the second and **keep billing until you destroy
them**. Order of magnitude in a cheaper region: roughly US$0.20 an hour for the
set, so about $5 a day or $15 for a three-day run — check the [EC2 pricing
page](https://aws.amazon.com/ec2/pricing/on-demand/) for your region rather than
trusting that number.

```bash
terraform destroy
```

## Design notes

**Ubuntu 24.04, not Amazon Linux.** The agent's only real input is
`/proc/pressure/memory`. Ubuntu compiles PSI in and enables it by default; on
some kernels the file is absent unless you boot with `psi=1`, and
`collector.py` returns `0.0` when it cannot read it — which would give you a
dashboard of flat zeroes with nothing to explain it. The bootstrap script checks
for the file and fails loudly instead.

**Security groups reference each other, not addresses.** The agents accept
traffic on their stress control port only from the dashboard's security group,
and the dashboard accepts metrics only from the agents' group. No IP addresses
are written down, so nothing drifts.

**`ssh_cidr` also gates the dashboard UI, and rejects `0.0.0.0/0`.** The
dashboard has no authentication and its `/run-stress` endpoint can load every
agent in the fleet, so anyone who can reach port 5000 can drive your instances.
One address only.

**A 512 MiB swapfile on the agents, by default.** With no swap, a `stress` run
on a `t3.micro` reaches the OOM killer almost at once: one spike and a dead
process. Swap gives the kernel something to reclaim, and reclaim stalls are
exactly what PSI measures. Set `swap_mb = 0` to turn it off.

**The agent is protected from the OOM killer** (`OOMScoreAdjust=-500`). The
thing watching the box should outlive the load it was asked to generate.

**IMDSv2 is required.** `instance.py` reads the instance type from the metadata
service, which is how the dashboard knows a box is a `t3.micro` — hostname alone
cannot tell you that. Reading your own metadata needs no IAM role and no
credentials, so no instance profile is created.

**Baselines are per instance.** Each agent calibrates its own idle PSI, because
a `t3.micro` idles differently from a `t3.large`. The dashboard's 2× and 5×
threshold lines are drawn from that, which is also why those lines only appear
when a single instance is selected.

## Known limit: history is in memory

`server.py` keeps samples in a bounded `deque` — one hour per instance — and
loses everything when the process restarts. That is fine for spot-checking a
load test. It is not a record you can scroll back through after two days, and
`terraform apply` replacing the server wipes it. Persisting to disk or a real
time-series store would be a separate change.

## Troubleshooting

**`ssh_cidr must be an IPv4 CIDR`** — `curl ifconfig.me` gave you an IPv6
address, and the `/32` went on the end of that. Instances in the default VPC get
IPv4 addresses, so the rules have to be IPv4:

```bash
curl -4 -s ifconfig.me; echo      # four dotted numbers, no colons
```

If `-4` returns nothing your connection is IPv6-only, and reaching an IPv4-only
instance needs a change beyond this config — enabling IPv6 on the VPC, subnet and
instances, or connecting from somewhere with IPv4.

Your address also changes when your network does, at which point the dashboard
stops loading. Rewrite `terraform.tfvars` and `apply` again; only the security
group rules change, and the instances are left alone.

An instance that never appears:

```bash
ssh ubuntu@<public-ip> 'sudo tail -50 /var/log/syshealth-bootstrap.log'
ssh ubuntu@<public-ip> 'sudo journalctl -u syshealth -n 50'
```

The dashboard itself:

```bash
ssh ubuntu@<dashboard-ip> 'sudo journalctl -u syshealth-server -n 50'
```

Reporting but flat at `0.00`: that is a genuinely idle box. Press **Stress
all**, or `stress --vm 2 --vm-bytes 800M --timeout 60s` over SSH.

Dashboard loads but the page is empty: check `curl localhost:5000/series` on the
server. If that 404s, the checked-out branch predates the graph.
