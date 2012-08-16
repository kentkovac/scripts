# Copyright (c) 2012 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

CGPT_PY="${BUILD_LIBRARY_DIR}/cgpt.py"
PARTITION_SCRIPT_PATH="usr/sbin/write_gpt.sh"

get_disk_layout_path() {
  DISK_LAYOUT_PATH="${BUILD_LIBRARY_DIR}/legacy_disk_layout.json"
  local partition_script_path=$(tempfile)
  for overlay in $(cros_overlay_list --board "$BOARD"); do
    local disk_layout="${overlay}/scripts/disk_layout.json"
    if [[ -e ${disk_layout} ]]; then
      DISK_LAYOUT_PATH=${disk_layout}
    fi
  done
}

emit_gpt_scripts() {
  local image="$1"
  local dir="$2"

  local pack="${dir}/pack_partitions.sh"
  local unpack="${dir}/unpack_partitions.sh"

  cat >"$unpack" <<HEADER
#!/bin/bash -eu
HEADER

  echo "# File automatically generated by $(basename $0)." >> "$unpack"

  cat >>"${unpack}" <<HEADER
# Do not edit.
TARGET=\${1:-}
if [[ -z "\$TARGET" ]]; then
  echo "Usage: \$0 DEVICE" 1>&2
  exit 1
fi
set -x
HEADER

  $GPT show "${image}" | sed -e 's/^/# /' >>"${unpack}"
  cp "${unpack}" "${pack}"

  $GPT show -q "${image}" |
    while read start size part x; do
      local file="part_${part}"
      local target="\"\${TARGET}\""
      local dd_args="bs=512 count=${size}"
      echo "dd if=${target} of=${file} ${dd_args} skip=${start}" >>"${unpack}"
      echo "dd if=${file} of=${target} ${dd_args} seek=${start} conv=notrunc" \
        >>"${pack}"
    done

  chmod +x "${unpack}" "${pack}"
}

write_partition_script() {
  local image_type=$1
  local partition_script_path=$2
  get_disk_layout_path

  sudo mkdir -p "$(dirname "${partition_script_path}")"

  sudo "${BUILD_LIBRARY_DIR}/cgpt.py" "write" \
    "${image_type}" "${DISK_LAYOUT_PATH}" "${partition_script_path}"
}

run_partition_script() {
  local outdev=$1
  local root_fs_img=$2

  local pmbr_img
  case ${ARCH} in
  arm)
    pmbr_img=/dev/zero
    ;;
  amd64|x86)
    pmbr_img=$(readlink -f /usr/share/syslinux/gptmbr.bin)
    ;;
  *)
    error "Unknown architecture: $ARCH"
    return 1
    ;;
  esac

  sudo mount -o loop "${root_fs_img}" "${root_fs_dir}"
  . "${root_fs_dir}/${PARTITION_SCRIPT_PATH}"
  write_partition_table "${outdev}" "${pmbr_img}"
  sudo umount "${root_fs_dir}"
}

get_fs_block_size() {
  get_disk_layout_path

  echo $(${CGPT_PY} readfsblocksize ${DISK_LAYOUT_PATH})
}

get_block_size() {
  get_disk_layout_path

  echo $(${CGPT_PY} readblocksize ${DISK_LAYOUT_PATH})
}

get_partition_size() {
  local image_type=$1
  local part_id=$2
  get_disk_layout_path

  echo $(${CGPT_PY} readpartsize ${image_type} ${DISK_LAYOUT_PATH} ${part_id})
}

get_filesystem_size() {
  local image_type=$1
  local part_id=$2
  get_disk_layout_path

  echo $(${CGPT_PY} readfssize ${image_type} ${DISK_LAYOUT_PATH} ${part_id})
}

get_label() {
  local image_type=$1
  local part_id=$2
  get_disk_layout_path

  echo $(${CGPT_PY} readlabel ${image_type} ${DISK_LAYOUT_PATH} ${part_id})
}

get_disk_layout_type() {
  DISK_LAYOUT_TYPE="base"
  if should_build_image ${CHROMEOS_FACTORY_INSTALL_SHIM_NAME}; then
    DISK_LAYOUT_TYPE="factory_install"
  fi
}

emit_gpt_scripts() {
  local image="$1"
  local dir="$2"

  local pack="${dir}/pack_partitions.sh"
  local unpack="${dir}/unpack_partitions.sh"

  cat >"${unpack}" <<HEADER
#!/bin/bash -eu
# File automatically generated. Do not edit.
TARGET=\${1:-}
if [[ -z "\$TARGET" ]]; then
  echo "Usage: \$0 DEVICE" 1>&2
  exit 1
fi
set -x
HEADER

  $GPT show "${image}" | sed -e 's/^/# /' >>"${unpack}"
  cp "${unpack}" "${pack}"

  $GPT show -q "${image}" |
    while read start size part x; do
      local file="part_${part}"
      local target="\"\$TARGET\""
      local dd_args="bs=512 count=${size}"
      echo "dd if=${target} of=${file} ${dd_args} skip=${start}" >>"${unpack}"
      echo "dd if=${file} of=${target} ${dd_args} seek=${start} conv=notrunc" \
        >>"${pack}"
    done

  chmod +x "${unpack}" "${pack}"
}

build_gpt() {
  local outdev="$1"
  local rootfs_img="$2"
  local stateful_img="$3"
  local esp_img="$4"

  get_disk_layout_type
  run_partition_script "${outdev}" "${rootfs_img}"

  local sudo=
  if [ ! -w "$outdev" ] ; then
    # use sudo when writing to a block device.
    sudo=sudo
  fi

  # Now populate the partitions.
  info "Copying stateful partition..."
  $sudo dd if="$stateful_img" of="$outdev" conv=notrunc bs=512 \
      seek=$(partoffset ${outdev} 1)

  info "Copying rootfs..."
  $sudo dd if="$rootfs_img" of="$outdev" conv=notrunc bs=512 \
      seek=$(partoffset ${outdev} 3)

  info "Copying EFI system partition..."
  $sudo dd if="$esp_img" of="$outdev" conv=notrunc bs=512 \
      seek=$(partoffset ${outdev} 12)

  # Pre-set "sucessful" bit in gpt, so we will never mark-for-death
  # a partition on an SDCard/USB stick.
  cgpt add -i 2 -S 1 "$outdev"
}