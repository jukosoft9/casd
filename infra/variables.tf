variable "subscription_id" {
  description = "Azure subscription to deploy into."
  type        = string
}

variable "image" {
  description = "Container image to run, pinned by digest (ghcr.io/<owner>/casd@sha256:...)."
  type        = string

  validation {
    condition     = can(regex("@sha256:[0-9a-f]{64}$", var.image))
    error_message = "Pin the image by digest so a deployment is reproducible."
  }
}

variable "name" {
  description = "Prefix for resource names."
  type        = string
  default     = "casd"
}

variable "location" {
  description = "Azure region."
  type        = string
  default     = "uksouth"
}

variable "cache_size_gb" {
  description = "Size of the file share. casd evicts at 90% of this."
  type        = number
  default     = 20
}

variable "allowed_cidrs" {
  description = "Source ranges allowed to reach the cache. Empty means anywhere."
  type        = list(string)
  default     = []
}
