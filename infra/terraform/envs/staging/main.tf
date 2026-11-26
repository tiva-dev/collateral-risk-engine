terraform {
  required_version = ">= 1.6"
  backend "gcs" {
    bucket = "tiva-cri-ops-cda9-tfstate"
    prefix = "cri/staging"
  }
}

provider "google" {
  project               = "tiva-cri-stg-cda9"
  region                = var.region
  user_project_override = true
  billing_project       = "tiva-cri-stg-cda9"
}

provider "google-beta" {
  project               = "tiva-cri-stg-cda9"
  region                = var.region
  user_project_override = true
  billing_project       = "tiva-cri-stg-cda9"
}

variable "region" {
  type    = string
  default = "europe-west1"
}

module "cri" {
  source          = "../../modules/cri-environment"
  environment     = "staging"
  project_id      = "tiva-cri-stg-cda9"
  ops_project_id  = "tiva-cri-ops-cda9"
  region          = var.region
  billing_account = "01BDBE-D3BDA3-260FCB"
  wif_pool_name   = var.wif_pool_name
  alert_emails    = ["gilchrist.e@tivafinance.com"]

  monthly_budget_usd       = 120
  db_tier                  = "db-g1-small"
  db_backup_retention_days = 7
  db_deletion_protection   = true
  api_max_instances        = 3
  worker_max_instances     = 2
  schedules_enabled        = true
  watchlist = [
    "NASDAQ:TSLA:USD:LISTED_EQUITY", "NASDAQ:NVDA:USD:LISTED_EQUITY", "NASDAQ:AAPL:USD:LISTED_EQUITY",
    "NYSE:SPY:USD:ETF", "NASDAQ:QQQ:USD:ETF",
    "NGX:MTNN:NGN:LISTED_EQUITY", "NGX:ZENITHBANK:NGN:LISTED_EQUITY", "NGX:GTCO:NGN:LISTED_EQUITY",
    "NGX:DANGCEM:NGN:LISTED_EQUITY", "NGX:AIRTELAFRI:NGN:LISTED_EQUITY",
  ]
}

variable "wif_pool_name" {
  type = string
}

output "api_url" { value = module.cri.api_url }
output "worker_url" { value = module.cri.worker_url }
output "deployer_service_account" { value = module.cri.deployer_service_account }
output "image_repository" { value = module.cri.image_repository }
