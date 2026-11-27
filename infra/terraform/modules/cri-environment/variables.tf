variable "environment" {
  description = "dev | staging | production"
  type        = string
  validation {
    condition     = contains(["dev", "staging", "production"], var.environment)
    error_message = "environment must be dev, staging or production"
  }
}

variable "project_id" { type = string }
variable "ops_project_id" { type = string }

variable "region" {
  type    = string
  default = "europe-west1"
}

variable "dr_region" {
  description = "Second region for backup exports (disaster recovery)."
  type        = string
  default     = "europe-west4"
}

variable "billing_account" { type = string }

variable "github_repository" {
  type    = string
  default = "tiva-dev/collateral-risk-engine"
}

variable "wif_pool_name" {
  description = "projects/<ops number>/locations/global/workloadIdentityPools/github"
  type        = string
}

variable "alert_emails" {
  type = list(string)
}

variable "monthly_budget_usd" {
  description = "Budget for this project; alerts at 50%, 90%, 100% actual and 100% forecast."
  type        = number
}

# Cloud SQL sizing. Staging uses a small shared-core instance; production a
# dedicated regional (high-availability) instance.
variable "db_tier" { type = string }

variable "db_availability" {
  type    = string
  default = "ZONAL"
}

variable "db_disk_gb" {
  type    = number
  default = 10
}

variable "db_backup_retention_days" {
  type    = number
  default = 7
}

variable "db_deletion_protection" {
  type    = bool
  default = true
}

variable "api_min_instances" {
  type    = number
  default = 0
}

variable "api_max_instances" {
  type    = number
  default = 3
}

variable "worker_max_instances" {
  type    = number
  default = 2
}

variable "schedules_enabled" {
  description = "Market-data ingestion, monitoring ticks and webhook sweeps."
  type        = bool
  default     = true
}

variable "watchlist" {
  description = "Instruments ingested even when no account holds them (stable keys)."
  type        = list(string)
  default     = []
}

# Hard deployment interlocks. Changing either to true is a Tiva decision that
# must go through review of the financial model and operational controls.
variable "lending_permitted" {
  type    = bool
  default = false
}

variable "execution_permitted" {
  type    = bool
  default = false
}

variable "placeholder_image" {
  description = "Used only until the first CI deployment; Terraform ignores later image changes."
  type        = string
  default     = "us-docker.pkg.dev/cloudrun/container/hello"
}

variable "db_credential_rotation" {
  description = "Rotation label for the database owner and app passwords. Changing it regenerates both passwords and their secret versions; redeploy afterwards. Empty means never rotated."
  type        = string
  default     = ""
}
