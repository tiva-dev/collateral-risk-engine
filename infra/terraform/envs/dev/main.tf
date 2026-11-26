terraform {
  required_version = ">= 1.6"
  backend "gcs" {
    bucket = "tiva-cri-ops-cda9-tfstate"
    prefix = "cri/dev"
  }
}

provider "google" {
  project               = "tiva-cri-dev-cda9"
  region                = var.region
  user_project_override = true
  billing_project       = "tiva-cri-dev-cda9"
}

provider "google-beta" {
  project               = "tiva-cri-dev-cda9"
  region                = var.region
  user_project_override = true
  billing_project       = "tiva-cri-dev-cda9"
}

variable "region" {
  type    = string
  default = "europe-west1"
}

module "cri" {
  source          = "../../modules/cri-environment"
  environment     = "dev"
  project_id      = "tiva-cri-dev-cda9"
  ops_project_id  = "tiva-cri-ops-cda9"
  region          = var.region
  billing_account = "01BDBE-D3BDA3-260FCB"
  wif_pool_name   = var.wif_pool_name
  alert_emails    = ["gilchrist.e@tivafinance.com"]

  monthly_budget_usd       = 40
  db_tier                  = "db-f1-micro"
  db_backup_retention_days = 3
  db_deletion_protection   = false
  api_max_instances        = 2
  worker_max_instances     = 1
  schedules_enabled        = false
}

variable "wif_pool_name" {
  type = string
}

output "api_url" { value = module.cri.api_url }
output "worker_url" { value = module.cri.worker_url }
output "deployer_service_account" { value = module.cri.deployer_service_account }
output "image_repository" { value = module.cri.image_repository }
