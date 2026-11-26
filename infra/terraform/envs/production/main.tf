terraform {
  required_version = ">= 1.6"
  backend "gcs" {
    bucket = "tiva-cri-ops-cda9-tfstate"
    prefix = "cri/production"
  }
}

provider "google" {
  project               = "tiva-cri-prd-cda9"
  region                = var.region
  user_project_override = true
  billing_project       = "tiva-cri-prd-cda9"
}

provider "google-beta" {
  project               = "tiva-cri-prd-cda9"
  region                = var.region
  user_project_override = true
  billing_project       = "tiva-cri-prd-cda9"
}

variable "region" {
  type    = string
  default = "europe-west1"
}

module "cri" {
  source          = "../../modules/cri-environment"
  environment     = "production"
  project_id      = "tiva-cri-prd-cda9"
  ops_project_id  = "tiva-cri-ops-cda9"
  region          = var.region
  billing_account = "01BDBE-D3BDA3-260FCB"
  wif_pool_name   = var.wif_pool_name
  alert_emails    = ["gilchrist.e@tivafinance.com"]

  # Production sizing: dedicated vCPUs, regional high availability, 30 backups.
  # Not applied until Tiva approves production spend (about USD 300/month).
  monthly_budget_usd       = 600
  db_tier                  = "db-custom-2-7680"
  db_availability          = "REGIONAL"
  db_disk_gb               = 50
  db_backup_retention_days = 30
  db_deletion_protection   = true
  api_min_instances        = 1
  api_max_instances        = 10
  worker_max_instances     = 4
  schedules_enabled        = true
  lending_permitted        = false
  execution_permitted      = false
}

variable "wif_pool_name" {
  type = string
}

output "api_url" { value = module.cri.api_url }
output "worker_url" { value = module.cri.worker_url }
output "deployer_service_account" { value = module.cri.deployer_service_account }
output "image_repository" { value = module.cri.image_repository }
