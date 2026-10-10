output "url" {
  description = "Base URL of the cache."
  value       = "https://${azurerm_container_app.this.ingress[0].fqdn}"
}

output "read_credentials" {
  description = "user:password for developers (read only)."
  value       = "dev:${random_password.read.result}"
  sensitive   = true
}

output "write_credentials" {
  description = "user:password for CI (read and write)."
  value       = "ci:${random_password.write.result}"
  sensitive   = true
}
