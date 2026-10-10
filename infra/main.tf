# casd on Azure Container Apps, with the blob directory on an Azure Files share.
#
# One replica only. casd keeps its eviction index in memory, so two replicas
# sharing a directory would each believe they own the size limit.

resource "azurerm_resource_group" "this" {
  name     = "${var.name}-rg"
  location = var.location
}

resource "random_string" "suffix" {
  length  = 6
  upper   = false
  special = false
}

resource "azurerm_storage_account" "this" {
  name                            = "${replace(var.name, "-", "")}${random_string.suffix.result}"
  resource_group_name             = azurerm_resource_group.this.name
  location                        = azurerm_resource_group.this.location
  account_tier                    = "Standard"
  account_replication_type        = "LRS" # a cache can be rebuilt; no need for geo-redundancy
  min_tls_version                 = "TLS1_2"
  allow_nested_items_to_be_public = false
}

resource "azurerm_storage_share" "cache" {
  name               = "cache"
  storage_account_id = azurerm_storage_account.this.id
  quota              = var.cache_size_gb
}

resource "azurerm_log_analytics_workspace" "this" {
  name                = "${var.name}-logs"
  resource_group_name = azurerm_resource_group.this.name
  location            = azurerm_resource_group.this.location
  sku                 = "PerGB2018"
  retention_in_days   = 30
}

resource "azurerm_container_app_environment" "this" {
  name                       = "${var.name}-env"
  resource_group_name        = azurerm_resource_group.this.name
  location                   = azurerm_resource_group.this.location
  log_analytics_workspace_id = azurerm_log_analytics_workspace.this.id
}

resource "azurerm_container_app_environment_storage" "cache" {
  name                         = "cache"
  container_app_environment_id = azurerm_container_app_environment.this.id
  account_name                 = azurerm_storage_account.this.name
  share_name                   = azurerm_storage_share.cache.name
  access_key                   = azurerm_storage_account.this.primary_access_key
  access_mode                  = "ReadWrite"
}

resource "random_password" "read" {
  length  = 32
  special = false
}

resource "random_password" "write" {
  length  = 32
  special = false
}

resource "azurerm_container_app" "this" {
  name                         = var.name
  resource_group_name          = azurerm_resource_group.this.name
  container_app_environment_id = azurerm_container_app_environment.this.id
  revision_mode                = "Single"

  secret {
    name  = "read-credentials"
    value = "dev:${random_password.read.result}"
  }

  secret {
    name  = "write-credentials"
    value = "ci:${random_password.write.result}"
  }

  template {
    min_replicas = 1
    max_replicas = 1

    volume {
      name         = "cache"
      storage_type = "AzureFile"
      storage_name = azurerm_container_app_environment_storage.cache.name
    }

    container {
      name   = "casd"
      image  = var.image
      cpu    = 0.5
      memory = "1Gi"
      # Leave 10% headroom: uploads in progress live on the same share.
      args = ["--max-size", "${floor(var.cache_size_gb * 0.9 * 1024)}M"]

      env {
        name        = "CASD_READ_CREDENTIALS"
        secret_name = "read-credentials"
      }

      env {
        name        = "CASD_WRITE_CREDENTIALS"
        secret_name = "write-credentials"
      }

      volume_mounts {
        name = "cache"
        path = "/data"
      }

      liveness_probe {
        transport = "HTTP"
        path      = "/healthz"
        port      = 8080
      }

      readiness_probe {
        transport = "HTTP"
        path      = "/healthz"
        port      = 8080
      }
    }
  }

  ingress {
    external_enabled = true
    target_port      = 8080

    traffic_weight {
      latest_revision = true
      percentage      = 100
    }

    dynamic "ip_security_restriction" {
      for_each = var.allowed_cidrs
      content {
        name             = "allow-${ip_security_restriction.key}"
        action           = "Allow"
        ip_address_range = ip_security_restriction.value
      }
    }
  }
}
