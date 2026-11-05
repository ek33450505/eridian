#!/usr/bin/env bash
# Shared root guard and delete helpers for setup.sh and train.sh. Source it;
# do not execute it. Provides:
#   mww_init <setup|train> "$@"
#                       resolve + validate MWW_ROOT (canonical) and handle
#                       `--check-root` (validate, report, exit; creates nothing).
#                       Only mode `setup` may create the root and the sentinel,
#                       and only when the root is new or empty (ignoring
#                       .DS_Store). Mode `train` requires an existing sentinel.
#   safe_rm_subdir REL  rm -rf a path relative to MWW_ROOT (may be nested).
#   safe_rm_download N  remove downloads/N and downloads/N.part.
#
# The root is canonicalised (cd -P / pwd -P) and must be strictly under the
# canonical $HOME. Every delete requires the sentinel in the canonical root and
# a relative subpath whose parent resolves under that root.

MWW_SENTINEL_NAME=".eridian-mww-root"

_mww_die() {
	echo "ERROR: $*" >&2
	exit 1
}

# mww_canon_root <raw>: prints the canonical form of <raw> WITHOUT creating
# anything. A not-yet-existing tail is appended to the canonical nearest
# existing ancestor (empty and '.' components dropped; '..' is refused).
mww_canon_root() {
	local raw="$1" anc tail="" part canon rest
	[[ -n "$raw" ]] || _mww_die "MWW_ROOT: empty value"
	[[ "$raw" == /* ]] || _mww_die "MWW_ROOT: must be an absolute path (got: '$raw')"
	if [[ "$raw" == */../* || "$raw" == */.. ]]; then
		_mww_die "MWW_ROOT: '..' components are not allowed (got: '$raw')"
	fi
	anc="$raw"
	while [[ ! -d "$anc" ]]; do
		anc="$(dirname -- "$anc")"
	done
	canon="$(cd -P -- "$anc" && pwd -P)" || _mww_die "MWW_ROOT: cannot canonicalise '$anc'"
	rest="${raw#"$anc"}"
	local IFS=/
	for part in $rest; do
		[[ -z "$part" || "$part" == "." ]] && continue
		tail="$tail/$part"
	done
	if [[ "$canon" == "/" ]]; then
		printf '%s\n' "${tail:-/}"
	else
		printf '%s\n' "$canon$tail"
	fi
}

# mww_root_state <canon>: new | empty | sentinel | nonempty
mww_root_state() {
	local canon="$1"
	if [[ ! -d "$canon" ]]; then
		echo new
	elif [[ -e "$canon/$MWW_SENTINEL_NAME" ]]; then
		echo sentinel
	elif [[ -z "$(find "$canon" -mindepth 1 -maxdepth 1 ! -name .DS_Store -print -quit)" ]]; then
		echo empty
	else
		echo nonempty
	fi
}

mww_init() {
	local mode="${1:-}" canon home_c state
	shift || true
	[[ "$mode" == setup || "$mode" == train ]] || _mww_die "mww_init: mode must be setup or train"

	canon="$(mww_canon_root "${MWW_ROOT-$HOME/Datasets/eridian-training/mww}")" || exit 1
	home_c="$(cd -P -- "$HOME" && pwd -P)" || _mww_die "MWW_ROOT: cannot canonicalise \$HOME"
	[[ "$home_c" != "/" ]] || _mww_die "MWW_ROOT: \$HOME resolves to '/'"
	[[ "$canon" != "/" ]] || _mww_die "MWW_ROOT: resolves to '/'"
	[[ "$canon" != "$home_c" ]] || _mww_die "MWW_ROOT: resolves to \$HOME itself ($home_c)"
	[[ "$canon" == "$home_c"/* ]] || _mww_die "MWW_ROOT: '$canon' is not strictly under \$HOME ($home_c)"

	state="$(mww_root_state "$canon")"
	case "$state:$mode" in
	nonempty:*)
		_mww_die "MWW_ROOT: '$canon' is a non-empty directory without the $MWW_SENTINEL_NAME sentinel; refusing to adopt it"
		;;
	new:train | empty:train)
		_mww_die "MWW_ROOT: '$canon' is not an initialised root (no sentinel); run setup.sh first"
		;;
	esac

	if [[ "${1:-}" == "--check-root" ]]; then
		echo "OK: $canon (state: $state)"
		exit 0
	fi

	MWW_ROOT="$canon"
	MWW_SENTINEL="$MWW_ROOT/$MWW_SENTINEL_NAME"
	if [[ "$mode" == setup && "$state" != sentinel ]]; then
		mkdir -p -- "$MWW_ROOT" || _mww_die "MWW_ROOT: cannot create '$MWW_ROOT'"
		: >"$MWW_SENTINEL"
	fi
}

_mww_require_sentinel() {
	[[ -n "${MWW_ROOT:-}" && -e "$MWW_ROOT/$MWW_SENTINEL_NAME" ]] ||
		_mww_die "refusing rm: sentinel missing in '${MWW_ROOT:-}'"
}

# safe_rm_subdir <rel>: relative, no leading '/', no empty/'.'/'..' components,
# no trailing '/'; the parent must resolve (cd -P) to the root or below it.
safe_rm_subdir() {
	local rel="${1:-}" parent parent_c base
	_mww_require_sentinel
	if [[ -z "$rel" || "$rel" == /* || "$rel" == */ || "/$rel/" == *"//"* ||
		"/$rel/" == *"/./"* || "/$rel/" == *"/../"* ]]; then
		_mww_die "refusing rm: unsafe subpath '$rel'"
	fi
	base="${rel##*/}"
	parent="$MWW_ROOT/${rel%"$base"}"
	parent="${parent%/}"
	[[ -d "$parent" ]] || return 0 # parent absent: nothing to remove
	parent_c="$(cd -P -- "$parent" && pwd -P)" || _mww_die "refusing rm: cannot resolve '$parent'"
	if [[ "$parent_c" != "$MWW_ROOT" && "$parent_c" != "$MWW_ROOT"/* ]]; then
		_mww_die "refusing rm: '$rel' resolves outside $MWW_ROOT"
	fi
	rm -rf -- "${parent_c:?}/${base:?}"
}

# safe_rm_download <name>: bare filename under $MWW_ROOT/downloads. Goes
# through safe_rm_subdir, so the parent-symlink check applies.
safe_rm_download() {
	local name="${1:-}"
	if [[ -z "$name" || "$name" == "." || "$name" == *..* || "$name" == */* ]]; then
		_mww_die "refusing rm: unsafe download name '$name'"
	fi
	safe_rm_subdir "downloads/$name"
	safe_rm_subdir "downloads/$name.part"
}
