terraform {
  required_version = ">= 1.6"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 6.14, < 8.0"
    }
    google-beta = {
      source  = "hashicorp/google-beta"
      version = ">= 6.14, < 8.0"
    }
    random = {
      source  = "hashicorp/random"
      version = ">= 3.6"
    }
  }
}

locals {
  env        = var.environment
  prefix     = "cri"
  is_prod    = var.environment == "production"
  ar_host    = "${var.region}-docker.pkg.dev"
  image_repo = "${local.ar_host}/${var.ops_project_id}/cri"
  labels = {
    system      = "cri"
    environment = var.environment
    managed_by  = "terraform"
  }
  services = [
    "artifactregistry.googleapis.com",
    "cloudscheduler.googleapis.com",
    "cloudtasks.googleapis.com",
    "compute.googleapis.com",
    "iam.googleapis.com",
    "iamcredentials.googleapis.com",
    "logging.googleapis.com",
    "monitoring.googleapis.com",
    "pubsub.googleapis.com",
    "run.googleapis.com",
    "secretmanager.googleapis.com",
    "servicenetworking.googleapis.com",
    "sqladmin.googleapis.com",
    "storage.googleapis.com",
    "billingbudgets.googleapis.com",
    "cloudresourcemanager.googleapis.com",
  ]
}

data "google_project" "this" {
  project_id = var.project_id
}

resource "google_project_service" "apis" {
  for_each                   = toset(local.services)
  project                    = var.project_id
  service                    = each.value
  disable_on_destroy         = false
  disable_dependent_services = false
}

# ------------------------------------------------------------------ identities

resource "google_service_account" "api" {
  project      = var.project_id
  account_id   = "cri-api"
  display_name = "CRI API (Cloud Run)"
  depends_on   = [google_project_service.apis]
}

resource "google_service_account" "worker" {
  project      = var.project_id
  account_id   = "cri-worker"
  display_name = "CRI worker: ingestion, monitoring, webhooks (Cloud Run)"
  depends_on   = [google_project_service.apis]
}

resource "google_service_account" "jobs" {
  project      = var.project_id
  account_id   = "cri-jobs"
  display_name = "CRI jobs: migrations, research, backtests (Cloud Run Jobs)"
  depends_on   = [google_project_service.apis]
}

# Identity that Cloud Scheduler, Cloud Tasks and Pub/Sub use to call the worker.
resource "google_service_account" "invoker" {
  project      = var.project_id
  account_id   = "cri-invoker"
  display_name = "CRI internal invoker (Scheduler, Tasks, Pub/Sub push)"
  depends_on   = [google_project_service.apis]
}

resource "google_service_account" "deployer" {
  project      = var.project_id
  account_id   = "cri-deployer"
  display_name = "CRI deployer (GitHub Actions via Workload Identity Federation)"
  depends_on   = [google_project_service.apis]
}

# GitHub jobs running in the GitHub environment named after this environment
# (dev, staging, production) can impersonate the deployer, and only those.
locals {
  # Production deploys are accepted only from the main branch.
  deployer_principal = local.is_prod ? "principalSet://iam.googleapis.com/${var.wif_pool_name}/attribute.env_ref/production@refs/heads/main" : "principalSet://iam.googleapis.com/${var.wif_pool_name}/attribute.environment/${var.environment}"
}

resource "google_service_account_iam_member" "deployer_wif" {
  service_account_id = google_service_account.deployer.name
  role               = "roles/iam.workloadIdentityUser"
  member             = local.deployer_principal
}

resource "google_project_iam_member" "deployer_roles" {
  for_each = toset([
    "roles/run.developer",
    "roles/logging.viewer",
    "roles/monitoring.viewer",
  ])
  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.deployer.email}"
}

resource "google_service_account_iam_member" "deployer_acts_as" {
  for_each           = { api = google_service_account.api.name, worker = google_service_account.worker.name, jobs = google_service_account.jobs.name }
  service_account_id = each.value
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.deployer.email}"
}



resource "google_project_iam_member" "runtime_roles" {
  for_each = {
    "api-log"       = ["roles/logging.logWriter", google_service_account.api.email]
    "api-metric"    = ["roles/monitoring.metricWriter", google_service_account.api.email]
    "api-trace"     = ["roles/cloudtrace.agent", google_service_account.api.email]
    "api-tasks"     = ["roles/cloudtasks.enqueuer", google_service_account.api.email]
    "worker-log"    = ["roles/logging.logWriter", google_service_account.worker.email]
    "worker-metric" = ["roles/monitoring.metricWriter", google_service_account.worker.email]
    "worker-tasks"  = ["roles/cloudtasks.enqueuer", google_service_account.worker.email]
    "jobs-log"      = ["roles/logging.logWriter", google_service_account.jobs.email]
    "jobs-metric"   = ["roles/monitoring.metricWriter", google_service_account.jobs.email]
  }
  project = var.project_id
  role    = each.value[0]
  member  = "serviceAccount:${each.value[1]}"
}

# The API and worker attach an OIDC token for the invoker when creating Cloud Tasks.
resource "google_service_account_iam_member" "enqueuers_act_as_invoker" {
  for_each           = { api = google_service_account.api.email, worker = google_service_account.worker.email }
  service_account_id = google_service_account.invoker.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${each.value}"
}

resource "google_project_service_identity" "pubsub" {
  provider = google-beta
  project  = var.project_id
  service  = "pubsub.googleapis.com"
}

resource "google_service_account_iam_member" "pubsub_token_creator" {
  service_account_id = google_service_account.invoker.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:${google_project_service_identity.pubsub.email}"
}

# ------------------------------------------------------------------- network

resource "google_compute_network" "vpc" {
  project                 = var.project_id
  name                    = "${local.prefix}-vpc"
  auto_create_subnetworks = false
  depends_on              = [google_project_service.apis]
}

resource "google_compute_subnetwork" "run" {
  project                  = var.project_id
  name                     = "${local.prefix}-run-${var.region}"
  region                   = var.region
  network                  = google_compute_network.vpc.id
  ip_cidr_range            = "10.20.0.0/24"
  private_ip_google_access = true
}

resource "google_compute_global_address" "psa" {
  project       = var.project_id
  name          = "${local.prefix}-psa"
  purpose       = "VPC_PEERING"
  address_type  = "INTERNAL"
  prefix_length = 20
  network       = google_compute_network.vpc.id
}

resource "google_service_networking_connection" "psa" {
  network                 = google_compute_network.vpc.id
  service                 = "servicenetworking.googleapis.com"
  reserved_peering_ranges = [google_compute_global_address.psa.name]
  deletion_policy         = "ABANDON"
}

# Outbound internet (market-data providers, client webhooks) from Cloud Run
# goes through Google's default egress; only private ranges use the VPC.

# ------------------------------------------------------------------ database

resource "random_password" "db_owner" {
  length  = 32
  special = false
}

resource "random_password" "db_app" {
  length  = 32
  special = false
}

resource "google_sql_database_instance" "main" {
  project             = var.project_id
  name                = "${local.prefix}-${local.env}-pg"
  region              = var.region
  database_version    = "POSTGRES_16"
  deletion_protection = var.db_deletion_protection

  settings {
    edition           = "ENTERPRISE"
    tier              = var.db_tier
    availability_type = var.db_availability
    disk_type         = "PD_SSD"
    disk_size         = var.db_disk_gb
    disk_autoresize   = true
    user_labels       = local.labels

    ip_configuration {
      ipv4_enabled                                  = false
      private_network                               = google_compute_network.vpc.id
      enable_private_path_for_google_cloud_services = true
      ssl_mode                                      = "ENCRYPTED_ONLY"
    }

    backup_configuration {
      enabled                        = true
      start_time                     = "02:00"
      point_in_time_recovery_enabled = true
      transaction_log_retention_days = min(7, var.db_backup_retention_days)
      location                       = var.dr_region
      backup_retention_settings {
        retained_backups = var.db_backup_retention_days
        retention_unit   = "COUNT"
      }
    }

    maintenance_window {
      day          = 7
      hour         = 3
      update_track = "stable"
    }

    insights_config {
      query_insights_enabled  = true
      record_application_tags = false
      record_client_address   = false
    }

    database_flags {
      name  = "log_min_duration_statement"
      value = "1000"
    }
  }

  depends_on = [google_service_networking_connection.psa]
}

resource "google_sql_database" "cri" {
  project  = var.project_id
  instance = google_sql_database_instance.main.name
  name     = "cri"
}

# Owner role: runs migrations (owns tables, so it is not subject to row-level security).
resource "google_sql_user" "owner" {
  project  = var.project_id
  instance = google_sql_database_instance.main.name
  name     = "cri_owner"
  password = random_password.db_owner.result
}

# Application role: not a table owner, so Postgres row-level security applies.
resource "google_sql_user" "app" {
  project  = var.project_id
  instance = google_sql_database_instance.main.name
  name     = "cri_app"
  password = random_password.db_app.result
}

locals {
  db_host      = google_sql_database_instance.main.private_ip_address
  db_url_owner = "postgresql://cri_owner:${random_password.db_owner.result}@${local.db_host}:5432/cri?sslmode=require"
  db_url_app   = "postgresql://cri_app:${random_password.db_app.result}@${local.db_host}:5432/cri?sslmode=require"
  worker_url   = google_cloud_run_v2_service.worker.uri
  queue_path   = "projects/${var.project_id}/locations/${var.region}/queues/${google_cloud_tasks_queue.webhooks.name}"
  topic_path   = google_pubsub_topic.market_data.id
  common_env = {
    CRI_ENV                 = local.env
    CRI_REGION              = var.region
    GOOGLE_CLOUD_PROJECT    = var.project_id
    CRI_LENDING_PERMITTED   = tostring(var.lending_permitted)
    CRI_EXECUTION_PERMITTED = tostring(var.execution_permitted)
    CRI_WEBHOOK_QUEUE       = local.queue_path
    CRI_TASKS_INVOKER_SA    = google_service_account.invoker.email
    CRI_MARKET_DATA_TOPIC   = local.topic_path
    CRI_WATCHLIST           = join(",", var.watchlist)
    CRI_AUTO_MIGRATE        = "false"
  }
}

# ------------------------------------------------------------------- secrets

resource "random_password" "pepper" {
  length  = 48
  special = false
}

resource "random_password" "bootstrap_admin" {
  length  = 43
  special = false
}

resource "random_password" "bootstrap_sandbox" {
  length  = 43
  special = false
}

locals {
  generated_secrets = {
    "cri-database-url-app"   = local.db_url_app
    "cri-database-url-owner" = local.db_url_owner
    "cri-api-key-pepper"     = random_password.pepper.result
    "cri-admin-api-key"      = "cri_${substr(local.env, 0, 4)}_${random_password.bootstrap_admin.result}"
    "cri-sandbox-api-key"    = "cri_${substr(local.env, 0, 4)}_${random_password.bootstrap_sandbox.result}"
  }
  # Provider credentials: containers only; versions are added from GitHub
  # secrets by the "Sync provider secrets" workflow, never committed.
  provider_secrets = ["alpaca-api-key", "alpaca-secret-key", "ngnmarket-api-key", "alpha-vantage-api-key"]
}

resource "google_secret_manager_secret" "generated" {
  for_each  = local.generated_secrets
  project   = var.project_id
  secret_id = each.key
  labels    = local.labels
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_version" "generated" {
  for_each    = local.generated_secrets
  secret      = google_secret_manager_secret.generated[each.key].id
  secret_data = each.value
}

resource "google_secret_manager_secret" "provider" {
  for_each  = toset(local.provider_secrets)
  project   = var.project_id
  secret_id = each.value
  labels    = local.labels
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

# Cloud Run refuses to start a revision whose secret has no version, so each
# provider secret starts with a placeholder that the sync workflow supersedes.
resource "google_secret_manager_secret_version" "provider_placeholder" {
  for_each    = toset(local.provider_secrets)
  secret      = google_secret_manager_secret.provider[each.value].id
  secret_data = "UNSET"
  lifecycle {
    ignore_changes = [secret_data, enabled]
  }
}

resource "google_secret_manager_secret_iam_member" "access" {
  for_each = {
    "api-db"         = ["cri-database-url-app", google_service_account.api.email]
    "api-pepper"     = ["cri-api-key-pepper", google_service_account.api.email]
    "worker-db"      = ["cri-database-url-app", google_service_account.worker.email]
    "worker-pepper"  = ["cri-api-key-pepper", google_service_account.worker.email]
    "jobs-db-owner"  = ["cri-database-url-owner", google_service_account.jobs.email]
    "jobs-db"        = ["cri-database-url-app", google_service_account.jobs.email]
    "jobs-pepper"    = ["cri-api-key-pepper", google_service_account.jobs.email]
    "jobs-admin"     = ["cri-admin-api-key", google_service_account.jobs.email]
    "jobs-sandbox"   = ["cri-sandbox-api-key", google_service_account.jobs.email]
    "deploy-sandbox" = ["cri-sandbox-api-key", google_service_account.deployer.email]
    "deploy-admin"   = ["cri-admin-api-key", google_service_account.deployer.email]
  }
  project   = var.project_id
  secret_id = google_secret_manager_secret.generated[each.value[0]].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${each.value[1]}"
}

resource "google_secret_manager_secret_iam_member" "provider_access" {
  for_each  = { for pair in setproduct(local.provider_secrets, ["worker", "jobs"]) : "${pair[0]}-${pair[1]}" => pair }
  project   = var.project_id
  secret_id = google_secret_manager_secret.provider[each.value[0]].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${each.value[1] == "worker" ? google_service_account.worker.email : google_service_account.jobs.email}"
}

resource "google_secret_manager_secret_iam_member" "provider_writer" {
  for_each  = toset(local.provider_secrets)
  project   = var.project_id
  secret_id = google_secret_manager_secret.provider[each.value].secret_id
  role      = "roles/secretmanager.secretVersionAdder"
  member    = "serviceAccount:${google_service_account.deployer.email}"
}

# ------------------------------------------------------------------- storage

resource "google_storage_bucket" "research" {
  project                     = var.project_id
  name                        = "${var.project_id}-research"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  labels                      = local.labels
  versioning {
    enabled = true
  }
  lifecycle_rule {
    condition {
      num_newer_versions = 5
    }
    action {
      type = "Delete"
    }
  }
}

# Daily logical exports of the database to a second region (disaster recovery,
# independent of Cloud SQL's own backups).
resource "google_storage_bucket" "db_exports" {
  project                     = var.project_id
  name                        = "${var.project_id}-db-exports"
  location                    = var.dr_region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  labels                      = local.labels
  versioning {
    enabled = true
  }
  lifecycle_rule {
    condition {
      age = local.is_prod ? 400 : 35
    }
    action {
      type = "Delete"
    }
  }
}

resource "google_storage_bucket_iam_member" "research_writers" {
  for_each = { jobs = google_service_account.jobs.email }
  bucket   = google_storage_bucket.research.name
  role     = "roles/storage.objectAdmin"
  member   = "serviceAccount:${each.value}"
}

resource "google_storage_bucket_iam_member" "sql_export_writer" {
  bucket = google_storage_bucket.db_exports.name
  role   = "roles/storage.objectCreator"
  member = "serviceAccount:${google_sql_database_instance.main.service_account_email_address}"
}

resource "google_project_iam_member" "invoker_sql_export" {
  project = var.project_id
  role    = "roles/cloudsql.viewer"
  member  = "serviceAccount:${google_service_account.invoker.email}"
}

resource "google_project_iam_custom_role" "sql_exporter" {
  project     = var.project_id
  role_id     = "criSqlExporter"
  title       = "CRI Cloud SQL exporter"
  permissions = ["cloudsql.instances.export"]
}

resource "google_project_iam_member" "invoker_sql_exporter" {
  project = var.project_id
  role    = google_project_iam_custom_role.sql_exporter.id
  member  = "serviceAccount:${google_service_account.invoker.email}"
}

# ------------------------------------------------------------ messaging

resource "google_pubsub_topic" "market_data" {
  project                    = var.project_id
  name                       = "cri-market-data"
  labels                     = local.labels
  message_retention_duration = "86400s"
  depends_on                 = [google_project_service.apis]
}

resource "google_pubsub_topic" "market_data_dlq" {
  project    = var.project_id
  name       = "cri-market-data-dead-letter"
  labels     = local.labels
  depends_on = [google_project_service.apis]
}

resource "google_pubsub_topic_iam_member" "worker_publishes" {
  project = var.project_id
  topic   = google_pubsub_topic.market_data.name
  role    = "roles/pubsub.publisher"
  member  = "serviceAccount:${google_service_account.worker.email}"
}

resource "google_pubsub_subscription" "market_data_push" {
  project              = var.project_id
  name                 = "cri-market-data-to-worker"
  topic                = google_pubsub_topic.market_data.id
  ack_deadline_seconds = 300
  labels               = local.labels
  push_config {
    push_endpoint = "${local.worker_url}/internal/pubsub/market-data"
    oidc_token {
      service_account_email = google_service_account.invoker.email
      audience              = local.worker_url
    }
  }
  retry_policy {
    minimum_backoff = "10s"
    maximum_backoff = "600s"
  }
  dead_letter_policy {
    dead_letter_topic     = google_pubsub_topic.market_data_dlq.id
    max_delivery_attempts = 10
  }
  expiration_policy {
    ttl = ""
  }
}

resource "google_pubsub_topic_iam_member" "dlq_publisher" {
  project = var.project_id
  topic   = google_pubsub_topic.market_data_dlq.name
  role    = "roles/pubsub.publisher"
  member  = "serviceAccount:${google_project_service_identity.pubsub.email}"
}

resource "google_pubsub_subscription_iam_member" "dlq_subscriber" {
  project      = var.project_id
  subscription = google_pubsub_subscription.market_data_push.name
  role         = "roles/pubsub.subscriber"
  member       = "serviceAccount:${google_project_service_identity.pubsub.email}"
}

resource "google_cloud_tasks_queue" "webhooks" {
  project  = var.project_id
  name     = "cri-webhooks"
  location = var.region
  rate_limits {
    max_dispatches_per_second = 20
    max_concurrent_dispatches = 10
  }
  # One attempt per task: the worker records failures and the scheduled sweep
  # owns retries with exponential backoff, so attempts are never double-counted.
  retry_config {
    max_attempts = 1
  }
  depends_on = [google_project_service.apis]
}

# ---------------------------------------------------------------- Cloud Run

resource "google_cloud_run_v2_service" "worker" {
  project             = var.project_id
  name                = "cri-worker"
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_ALL"
  deletion_protection = local.is_prod
  labels              = local.labels

  template {
    service_account                  = google_service_account.worker.email
    timeout                          = "900s"
    max_instance_request_concurrency = 4
    scaling {
      min_instance_count = 0
      max_instance_count = var.worker_max_instances
    }
    vpc_access {
      network_interfaces {
        network    = google_compute_network.vpc.id
        subnetwork = google_compute_subnetwork.run.id
        tags       = ["cri-run"]
      }
      egress = "PRIVATE_RANGES_ONLY"
    }
    containers {
      image = var.placeholder_image
      resources {
        limits = {
          cpu    = "1"
          memory = "1Gi"
        }
        cpu_idle = true
      }
      dynamic "env" {
        for_each = merge(local.common_env, { CRI_SERVICE_ROLE = "worker" })
        content {
          name  = env.key
          value = env.value
        }
      }
      dynamic "env" {
        for_each = {
          CRI_DATABASE_URL   = "cri-database-url-app"
          CRI_API_KEY_PEPPER = "cri-api-key-pepper"
        }
        content {
          name = env.key
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.generated[env.value].secret_id
              version = "latest"
            }
          }
        }
      }
      dynamic "env" {
        for_each = {
          ALPACA_API_KEY        = "alpaca-api-key"
          ALPACA_SECRET_KEY     = "alpaca-secret-key"
          NGNMARKET_API_KEY     = "ngnmarket-api-key"
          ALPHA_VANTAGE_API_KEY = "alpha-vantage-api-key"
        }
        content {
          name = env.key
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.provider[env.value].secret_id
              version = "latest"
            }
          }
        }
      }
      startup_probe {
        http_get {
          path = "/health"
        }
        period_seconds    = 5
        failure_threshold = 12
      }
    }
  }

  lifecycle {
    ignore_changes = [
      template[0].containers[0].image,
      # template[0].revision is deliberately NOT ignored: `gcloud run deploy`
      # writes a client-generated revision name into the template, and an
      # ignored name is re-sent on every later Terraform update, which Cloud
      # Run rejects (409 "revision ... with different configuration already
      # exists"). Leaving it unset lets Cloud Run auto-name the new revision.
      template[0].labels,
      client,
      client_version,
    ]
  }

  depends_on = [
    google_secret_manager_secret_iam_member.access,
    google_secret_manager_secret_iam_member.provider_access,
    google_secret_manager_secret_version.generated,
    google_secret_manager_secret_version.provider_placeholder,
  ]
}

resource "google_cloud_run_v2_service_iam_member" "worker_invoker" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.worker.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.invoker.email}"
}

resource "google_cloud_run_v2_service" "api" {
  project             = var.project_id
  name                = "cri-api"
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_ALL"
  deletion_protection = local.is_prod
  labels              = local.labels
  # Public endpoint without an allUsers IAM grant (compatible with domain-
  # restricted sharing). Every request is authenticated by the application
  # with a tenant API key; /health is the only unauthenticated route.
  invoker_iam_disabled = true

  template {
    service_account                  = google_service_account.api.email
    timeout                          = "60s"
    max_instance_request_concurrency = 8
    scaling {
      min_instance_count = var.api_min_instances
      max_instance_count = var.api_max_instances
    }
    vpc_access {
      network_interfaces {
        network    = google_compute_network.vpc.id
        subnetwork = google_compute_subnetwork.run.id
        tags       = ["cri-run"]
      }
      egress = "PRIVATE_RANGES_ONLY"
    }
    containers {
      image = var.placeholder_image
      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
        cpu_idle = true
      }
      dynamic "env" {
        for_each = merge(local.common_env, { CRI_SERVICE_ROLE = "api", CRI_WORKER_URL = local.worker_url })
        content {
          name  = env.key
          value = env.value
        }
      }
      dynamic "env" {
        for_each = {
          CRI_DATABASE_URL   = "cri-database-url-app"
          CRI_API_KEY_PEPPER = "cri-api-key-pepper"
        }
        content {
          name = env.key
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.generated[env.value].secret_id
              version = "latest"
            }
          }
        }
      }
      startup_probe {
        http_get {
          path = "/health"
        }
        period_seconds    = 5
        failure_threshold = 12
      }
      liveness_probe {
        http_get {
          path = "/health"
        }
        period_seconds = 30
      }
    }
  }

  lifecycle {
    ignore_changes = [
      template[0].containers[0].image,
      # template[0].revision is deliberately NOT ignored: `gcloud run deploy`
      # writes a client-generated revision name into the template, and an
      # ignored name is re-sent on every later Terraform update, which Cloud
      # Run rejects (409 "revision ... with different configuration already
      # exists"). Leaving it unset lets Cloud Run auto-name the new revision.
      template[0].labels,
      client,
      client_version,
    ]
  }

  depends_on = [google_secret_manager_secret_iam_member.access, google_secret_manager_secret_version.generated]
}

# --------------------------------------------------------------- Cloud Run Jobs

locals {
  jobs = {
    "cri-migrate" = {
      args    = ["python", "-m", "app.platform.cli", "bootstrap"]
      timeout = "600s"
      memory  = "512Mi"
      cpu     = "1"
      secrets = {
        CRI_DATABASE_URL          = ["generated", "cri-database-url-owner"]
        CRI_API_KEY_PEPPER        = ["generated", "cri-api-key-pepper"]
        CRI_BOOTSTRAP_ADMIN_KEY   = ["generated", "cri-admin-api-key"]
        CRI_BOOTSTRAP_SANDBOX_KEY = ["generated", "cri-sandbox-api-key"]
      }
    }
    "cri-research" = {
      args    = ["python", "-m", "research.methodology_lab.run_experiments"]
      timeout = "21600s"
      memory  = "8Gi"
      cpu     = "4"
      secrets = {
        ALPACA_API_KEY        = ["provider", "alpaca-api-key"]
        ALPACA_SECRET_KEY     = ["provider", "alpaca-secret-key"]
        NGNMARKET_API_KEY     = ["provider", "ngnmarket-api-key"]
        ALPHA_VANTAGE_API_KEY = ["provider", "alpha-vantage-api-key"]
      }
    }
    "cri-backtest" = {
      args    = ["python", "-m", "app.platform.cli", "backtest"]
      timeout = "3600s"
      memory  = "4Gi"
      cpu     = "2"
      secrets = {
        CRI_DATABASE_URL   = ["generated", "cri-database-url-app"]
        CRI_API_KEY_PEPPER = ["generated", "cri-api-key-pepper"]
      }
    }
  }
}

resource "google_cloud_run_v2_job" "jobs" {
  for_each            = local.jobs
  project             = var.project_id
  name                = each.key
  location            = var.region
  deletion_protection = false
  labels              = local.labels

  template {
    task_count = 1
    template {
      service_account = google_service_account.jobs.email
      timeout         = each.value.timeout
      max_retries     = each.key == "cri-migrate" ? 0 : 1
      vpc_access {
        network_interfaces {
          network    = google_compute_network.vpc.id
          subnetwork = google_compute_subnetwork.run.id
          tags       = ["cri-run"]
        }
        egress = "PRIVATE_RANGES_ONLY"
      }
      containers {
        image   = var.placeholder_image
        command = [each.value.args[0]]
        args    = slice(each.value.args, 1, length(each.value.args))
        resources {
          limits = {
            cpu    = each.value.cpu
            memory = each.value.memory
          }
        }
        dynamic "env" {
          for_each = merge(local.common_env, {
            CRI_SERVICE_ROLE          = "job"
            CRI_RESEARCH_BUCKET       = google_storage_bucket.research.name
            HISTORICAL_DATA_CACHE_DIR = "/tmp/cri-cache"
            SIMULATION_OUTPUT_DIR     = "/tmp/cri-out"
          })
          content {
            name  = env.key
            value = env.value
          }
        }
        dynamic "env" {
          for_each = each.value.secrets
          content {
            name = env.key
            value_source {
              secret_key_ref {
                secret  = env.value[0] == "generated" ? google_secret_manager_secret.generated[env.value[1]].secret_id : google_secret_manager_secret.provider[env.value[1]].secret_id
                version = "latest"
              }
            }
          }
        }
      }
    }
  }

  lifecycle {
    ignore_changes = [
      template[0].template[0].containers[0].image,
      template[0].labels,
      client,
      client_version,
    ]
  }

  depends_on = [
    google_secret_manager_secret_iam_member.access,
    google_secret_manager_secret_iam_member.provider_access,
    google_secret_manager_secret_version.generated,
    google_secret_manager_secret_version.provider_placeholder,
  ]
}

# -------------------------------------------------------------- schedules

locals {
  schedules = var.schedules_enabled ? {
    # NGX trades 10:00–14:30 WAT; US 15:30–22:00 WAT (14:30–21:00 in US winter time).
    "cri-ingest-market-data" = { schedule = "*/15 9-22 * * 1-5", path = "/internal/market-data/ingest", deadline = "600s" }
    "cri-monitoring-tick"    = { schedule = "*/5 * * * *", path = "/internal/monitoring/tick-all", deadline = "300s" }
    "cri-webhook-sweep"      = { schedule = "* * * * *", path = "/internal/webhooks/dispatch", deadline = "120s" }
    "cri-shadow-models"      = { schedule = "7,37 * * * *", path = "/internal/shadow/run-all", deadline = "900s" }
  } : {}
}

resource "google_cloud_scheduler_job" "worker" {
  for_each         = local.schedules
  project          = var.project_id
  region           = var.region
  name             = each.key
  schedule         = each.value.schedule
  time_zone        = "Africa/Lagos"
  attempt_deadline = each.value.deadline
  retry_config {
    retry_count = 1
  }
  http_target {
    http_method = "POST"
    uri         = "${local.worker_url}${each.value.path}"
    oidc_token {
      service_account_email = google_service_account.invoker.email
      audience              = local.worker_url
    }
  }
  depends_on = [google_project_service.apis]
}

resource "google_cloud_scheduler_job" "db_export" {
  project   = var.project_id
  region    = var.region
  name      = "cri-db-export"
  schedule  = "30 1 * * *"
  time_zone = "Africa/Lagos"
  http_target {
    http_method = "POST"
    uri         = "https://sqladmin.googleapis.com/v1/projects/${var.project_id}/instances/${google_sql_database_instance.main.name}/export"
    body = base64encode(jsonencode({
      exportContext = {
        fileType  = "SQL"
        uri       = "gs://${google_storage_bucket.db_exports.name}/cri-${local.env}-daily.sql.gz"
        databases = ["cri"]
        offload   = false
      }
    }))
    headers = { "Content-Type" = "application/json" }
    oauth_token {
      service_account_email = google_service_account.invoker.email
      scope                 = "https://www.googleapis.com/auth/cloud-platform"
    }
  }
  depends_on = [google_project_service.apis]
}

# --------------------------------------------------- logging, monitoring, alerts

resource "google_project_iam_audit_config" "data_access" {
  for_each = toset(["secretmanager.googleapis.com", "cloudsql.googleapis.com", "iam.googleapis.com"])
  project  = var.project_id
  service  = each.value
  audit_log_config {
    log_type = "ADMIN_READ"
  }
  audit_log_config {
    log_type = "DATA_READ"
  }
}

resource "google_monitoring_notification_channel" "email" {
  for_each     = toset(var.alert_emails)
  project      = var.project_id
  display_name = "CRI ${local.env} alerts ${each.value}"
  type         = "email"
  labels = {
    email_address = each.value
  }
  depends_on = [google_project_service.apis]
}

locals {
  channels = [for c in google_monitoring_notification_channel.email : c.id]
}

resource "google_monitoring_uptime_check_config" "api" {
  project      = var.project_id
  display_name = "CRI ${local.env} API /health"
  timeout      = "10s"
  period       = "300s"
  http_check {
    path         = "/health"
    port         = 443
    use_ssl      = true
    validate_ssl = true
  }
  monitored_resource {
    type = "uptime_url"
    labels = {
      project_id = var.project_id
      host       = trimprefix(google_cloud_run_v2_service.api.uri, "https://")
    }
  }
}

resource "google_monitoring_alert_policy" "uptime" {
  project      = var.project_id
  display_name = "CRI ${local.env}: API health check failing"
  combiner     = "OR"
  conditions {
    display_name = "uptime check failed"
    condition_threshold {
      filter          = "metric.type=\"monitoring.googleapis.com/uptime_check/check_passed\" AND resource.type=\"uptime_url\" AND metric.label.check_id=\"${google_monitoring_uptime_check_config.api.uptime_check_id}\""
      comparison      = "COMPARISON_GT"
      threshold_value = 1
      duration        = "600s"
      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_NEXT_OLDER"
        cross_series_reducer = "REDUCE_COUNT_FALSE"
        group_by_fields      = ["resource.label.*"]
      }
    }
  }
  notification_channels = local.channels
}

resource "google_monitoring_alert_policy" "errors_5xx" {
  project      = var.project_id
  display_name = "CRI ${local.env}: Cloud Run 5xx responses"
  combiner     = "OR"
  conditions {
    display_name = "5xx rate"
    condition_threshold {
      filter          = "metric.type=\"run.googleapis.com/request_count\" AND resource.type=\"cloud_run_revision\" AND metric.label.response_code_class=\"5xx\""
      comparison      = "COMPARISON_GT"
      threshold_value = 5
      duration        = "300s"
      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_SUM"
        cross_series_reducer = "REDUCE_SUM"
        group_by_fields      = ["resource.label.service_name"]
      }
    }
  }
  notification_channels = local.channels
}

resource "google_logging_metric" "job_failures" {
  project = var.project_id
  name    = "cri_job_failures"
  filter  = "resource.type=\"cloud_run_job\" AND severity>=ERROR AND protoPayload.methodName=\"/Jobs.RunJob\" OR (resource.type=\"cloud_run_job\" AND textPayload:\"Execution\" AND textPayload:\"failed\")"
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
  }
}

resource "google_logging_metric" "webhook_dead_letters" {
  project = var.project_id
  name    = "cri_webhook_dead_letters"
  filter  = "resource.type=\"cloud_run_revision\" AND jsonPayload.event=\"webhook_dead_letter\" OR textPayload:\"webhook_dead_letter\""
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
  }
}

resource "google_logging_metric" "ingestion_errors" {
  project = var.project_id
  name    = "cri_ingestion_errors"
  filter  = "resource.type=\"cloud_run_revision\" AND textPayload:\"market_data_ingestion_error\""
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
  }
}

resource "google_monitoring_alert_policy" "log_metrics" {
  for_each = {
    job_failures         = google_logging_metric.job_failures.name
    webhook_dead_letters = google_logging_metric.webhook_dead_letters.name
    ingestion_errors     = google_logging_metric.ingestion_errors.name
  }
  project      = var.project_id
  display_name = "CRI ${local.env}: ${replace(each.key, "_", " ")}"
  combiner     = "OR"
  conditions {
    display_name = each.key
    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/user/${each.value}\" AND resource.type=\"${each.key == "job_failures" ? "cloud_run_job" : "cloud_run_revision"}\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"
      aggregations {
        alignment_period   = "600s"
        per_series_aligner = "ALIGN_SUM"
      }
    }
  }
  notification_channels = local.channels
}

resource "google_monitoring_alert_policy" "sql" {
  for_each = {
    cpu  = ["cloudsql.googleapis.com/database/cpu/utilization", 0.8]
    disk = ["cloudsql.googleapis.com/database/disk/utilization", 0.8]
  }
  project      = var.project_id
  display_name = "CRI ${local.env}: Cloud SQL ${each.key} above 80%"
  combiner     = "OR"
  conditions {
    display_name = each.key
    condition_threshold {
      filter          = "metric.type=\"${each.value[0]}\" AND resource.type=\"cloudsql_database\""
      comparison      = "COMPARISON_GT"
      threshold_value = each.value[1]
      duration        = "900s"
      aggregations {
        alignment_period   = "300s"
        per_series_aligner = "ALIGN_MEAN"
      }
    }
  }
  notification_channels = local.channels
}

# ------------------------------------------------------------------ budget


# Cross-project grants and budgets moved to infra/bootstrap/harden.sh so that
# each environment's Terraform identity needs rights only in its own project.
# These blocks forget the old resources without deleting them.
removed {
  from = google_artifact_registry_repository_iam_member.deployer_push
  lifecycle {
    destroy = false
  }
}

removed {
  from = google_artifact_registry_repository_iam_member.run_agent_pull
  lifecycle {
    destroy = false
  }
}

removed {
  from = google_billing_budget.project
  lifecycle {
    destroy = false
  }
}

# ------------------------------------------------- egress lockdown (SSRF)
# Cloud Run sends private-range traffic into the VPC. Only PostgreSQL to the
# Cloud SQL peering range is allowed; every other private destination is
# denied, so a webhook or provider URL can never reach internal services even
# if application-level validation were bypassed.
resource "google_compute_firewall" "run_allow_sql" {
  project            = var.project_id
  name               = "cri-run-allow-sql"
  network            = google_compute_network.vpc.id
  direction          = "EGRESS"
  priority           = 900
  target_tags        = ["cri-run"]
  destination_ranges = ["${google_compute_global_address.psa.address}/${google_compute_global_address.psa.prefix_length}"]
  allow {
    protocol = "tcp"
    ports    = ["5432"]
  }
}

resource "google_compute_firewall" "run_deny_private" {
  project            = var.project_id
  name               = "cri-run-deny-private"
  network            = google_compute_network.vpc.id
  direction          = "EGRESS"
  priority           = 1000
  target_tags        = ["cri-run"]
  destination_ranges = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10"]
  deny {
    protocol = "all"
  }
  log_config {
    metadata = "INCLUDE_ALL_METADATA"
  }
}

# ------------------------------------- Control Centre and backtest access
resource "google_project_iam_member" "api_read_roles" {
  for_each = toset(["roles/run.viewer", "roles/cloudscheduler.viewer", "roles/monitoring.viewer"])
  project  = var.project_id
  role     = each.value
  member   = "serviceAccount:${google_service_account.api.email}"
}

resource "google_cloud_run_v2_job_iam_member" "api_runs_backtests" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_job.jobs["cri-backtest"].name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.api.email}"
}
