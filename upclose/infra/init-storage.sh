#!/bin/sh
set -eu
endpoint="http://floci:4566"
attempt=0
until aws --endpoint-url "$endpoint" s3api list-buckets >/dev/null 2>&1; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 30 ]; then echo "Local storage did not become ready" >&2; exit 1; fi
  sleep 2
done
if ! aws --endpoint-url "$endpoint" s3api head-bucket --bucket "$S3_BUCKET" >/dev/null 2>&1; then
  aws --endpoint-url "$endpoint" s3api create-bucket --bucket "$S3_BUCKET" >/dev/null
fi
# No bucket policy or public ACL: objects stay private in the application contract.
# AllowedOrigins is WEB_ORIGIN plus each comma-separated WEB_EXTRA_ORIGINS entry
# (explicit origins only, never "*"). Values are JSON-escaped before interpolation.
json_escape() {
  printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'
}
trim() {
  value=$1
  value=${value#"${value%%[![:space:]]*}"}
  value=${value%"${value##*[![:space:]]}"}
  printf '%s' "$value"
}
origins_json="\"$(json_escape "$WEB_ORIGIN")\""
if [ -n "${WEB_EXTRA_ORIGINS:-}" ]; then
  old_ifs=$IFS
  IFS=','
  # Word splitting of the comma-separated list is intentional here.
  # shellcheck disable=SC2086
  for extra in $WEB_EXTRA_ORIGINS; do
    IFS=$old_ifs
    extra=$(trim "$extra")
    if [ -n "$extra" ]; then
      origins_json="$origins_json,\"$(json_escape "$extra")\""
    fi
    IFS=','
  done
  IFS=$old_ifs
fi
aws --endpoint-url "$endpoint" s3api put-bucket-cors --bucket "$S3_BUCKET" --cors-configuration "{\"CORSRules\":[{\"AllowedOrigins\":[$origins_json],\"AllowedMethods\":[\"PUT\",\"GET\",\"HEAD\"],\"AllowedHeaders\":[\"*\"],\"ExposeHeaders\":[\"ETag\"],\"MaxAgeSeconds\":300}]}"
echo "Local storage bucket and browser CORS configured"
