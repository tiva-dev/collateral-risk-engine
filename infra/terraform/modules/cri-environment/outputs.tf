output "api_url" { value = google_cloud_run_v2_service.api.uri }
output "worker_url" { value = google_cloud_run_v2_service.worker.uri }
output "deployer_service_account" { value = google_service_account.deployer.email }
output "image_repository" { value = local.image_repo }
output "database_instance" { value = google_sql_database_instance.main.connection_name }
output "research_bucket" { value = google_storage_bucket.research.name }
output "db_exports_bucket" { value = google_storage_bucket.db_exports.name }
