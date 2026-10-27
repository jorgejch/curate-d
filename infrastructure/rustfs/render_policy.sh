#!/bin/sh
# Render a RustFS policy template for one bucket and prefix, onto stdout.
#
# usage: render_policy.sh TEMPLATE BUCKET PREFIX
#
# `rc` imports a policy as written and cannot template it, so rustfs-init renders the
# templates in policies/ here first. @BUCKET@ is the bucket, @PREFIX@ the prefix with its
# trailing slash and @PREFIX_KEY@ the prefix without it (see initialize.sh for why both
# are granted). The values are checked before they reach `sed` or the JSON: an unchecked
# one could widen a grant or break the document, and the grant is what scopes the identity.

set -eu
LC_ALL=C
export LC_ALL

die() { echo "render_policy: $*" >&2; exit 1; }

[ "$#" -eq 3 ] || die "usage: render_policy.sh TEMPLATE BUCKET PREFIX"
template="$1"
bucket="$2"
prefix="$3"

[ -f "$template" ] || die "no such template: ${template}"

# S3 bucket naming: 3-63 of lowercase letters, digits, dots and hyphens, alphanumeric ends.
case "$bucket" in
    *[!a-z0-9.-]* | [!a-z0-9]* | *[!a-z0-9]) die "invalid bucket name: ${bucket}" ;;
esac
[ "${#bucket}" -ge 3 ] && [ "${#bucket}" -le 63 ] || die "invalid bucket name: ${bucket}"

# The shape Settings.staging_s3_content_prefix requires, within a conservative key
# alphabet: relative, ending in '/', no empty path segment.
case "$prefix" in
    *[!A-Za-z0-9._/-]* | /* | *//*) die "invalid prefix: ${prefix}" ;;
    */) ;;
    *) die "invalid prefix: ${prefix} (must end with '/')" ;;
esac

sed -e "s|@BUCKET@|${bucket}|g" \
    -e "s|@PREFIX_KEY@|${prefix%/}|g" \
    -e "s|@PREFIX@|${prefix}|g" \
    "$template"
