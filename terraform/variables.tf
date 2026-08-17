variable "region" {
  description = "AWS region to launch into."
  type        = string
  default     = "eu-north-1"
}

variable "instance_types" {
  description = "The sizes to compare. Each becomes one instance running the same agent."
  type        = list(string)
  default     = ["t3.micro", "t3.small", "t3.medium", "t3.large"]
}

variable "server_instance_type" {
  description = "Size of the dashboard server. It only collects and serves JSON, so it does not need much."
  type        = string
  default     = "t3.small"
}

variable "key_name" {
  description = "Name of an existing EC2 key pair in this region, used for SSH."
  type        = string
}

variable "ssh_cidr" {
  description = "Who may SSH in and open the dashboard, in CIDR form (e.g. \"203.0.113.4/32\"). Deliberately has no default — do not open this to the world by accident."
  type        = string

  validation {
    condition     = var.ssh_cidr != "0.0.0.0/0"
    error_message = "Refusing 0.0.0.0/0. The dashboard exposes /run-stress with no authentication, so anyone reaching it could load your instances. Use your own address, e.g. \"203.0.113.4/32\"."
  }
}

variable "repo_url" {
  description = "Git repository the instances install from."
  type        = string
  default     = "https://github.com/aryangorde8/syshealth.git"
}

variable "repo_branch" {
  description = "Branch to check out on each instance."
  type        = string
  default     = "claude/syshealth-pressure-graph-btz8hm"
}

variable "swap_mb" {
  description = <<-EOT
    Swapfile size in MiB on the agents, or 0 for none. Worth keeping: with no
    swap, a stress run on a small instance hits the OOM killer almost
    immediately, so you see a spike and a dead process instead of sustained
    reclaim. Swap gives the kernel something to reclaim, which is what PSI
    actually measures.
  EOT
  type        = number
  default     = 512
}

variable "calibrate" {
  description = "Run the 60s idle baseline calibration at first boot. Without it every agent falls back to a 0.01 baseline, and the dashboard's 2x/5x threshold lines are guesses."
  type        = bool
  default     = true
}

variable "name_prefix" {
  description = "Prefix for instance and security group names."
  type        = string
  default     = "syshealth"
}

variable "tags" {
  description = "Extra tags applied to every resource."
  type        = map(string)
  default     = {}
}
