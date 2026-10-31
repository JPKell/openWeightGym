#!/usr/bin/env bash
# Build a newer llama.cpp beside the current one, and optionally switch LoadCoach to it.
#
# Shows the last ten commits of llama.cpp, lets you pick one, builds llama-server for it in its
# own folder, then offers to point the llama-server link at the new build and restart LoadCoach.
# The build you were using is left untouched, so going back is one command, printed at the end.
#
#   scripts/update-llamacpp.sh
set -euo pipefail

# ---- settings for this machine --------------------------------------------------------------
REPO=~/ai/tools/llama.cpp                       # the llama.cpp checkout; builds go in $REPO-NAME
BRANCH=master                                   # llama.cpp's main branch
LINK=~/.local/bin/llama-server                  # what `llama_server` in loadcoach.toml points at
NVCC=/usr/local/cuda-13.1/bin/nvcc              # the CUDA compiler
CUDA_ARCH=120                                   # 120 = RTX 50xx, 89 = RTX 40xx, 86 = RTX 30xx
OVERLAY=~/ai/tools/cuda-13.1-include-overlay    # works around the rsqrt compile error; used if present
SERVICE=loadcoach.service
HOW_MANY=10
JOBS=6                                          # compilers at once. Unlimited ran this machine out of memory
# ---------------------------------------------------------------------------------------------

ask() { read -rp "$1 [y/N] " answer && [[ $answer == [yY]* ]]; }

echo "Fetching the latest llama.cpp commits..."
# The checkout is a shallow one that follows a single tag, so ask for the branch by name, and
# only for as much history as is about to be shown.
git -C "$REPO" fetch --quiet --tags --depth $((HOW_MANY + 5)) origin "+refs/heads/$BRANCH:refs/remotes/origin/$BRANCH"
branch=origin/$BRANCH

# The commit the build in use was made from, as llama-server itself reports it.
current=$("$LINK" --version 2>&1 | grep -oP 'commit \K[0-9a-f]+' || true)

mapfile -t commits < <(git -C "$REPO" log -"$HOW_MANY" --format=%H "$branch")
names=()
echo
echo "The last $HOW_MANY commits on $branch, newest first:"
for i in "${!commits[@]}"; do
  commit=${commits[$i]}
  # A release has a tag such as b11445. Anything else is named by its short hash.
  name=$(git -C "$REPO" describe --tags --exact-match "$commit" 2>/dev/null || git -C "$REPO" rev-parse --short "$commit")
  names+=("$name")
  mark=""
  [[ -n $current && $commit == "$current"* ]] && mark="  <- in use now"
  [[ -x $REPO-$name/build/bin/llama-server ]] && mark+="  (already built)"
  printf '%3d) %-10s %s  %s%s\n' $((i + 1)) "$name" \
    "$(git -C "$REPO" log -1 --format=%cs "$commit")" \
    "$(git -C "$REPO" log -1 --format=%s "$commit" | cut -c1-70)" "$mark"
done
echo
read -rp "Which one? (1-${#commits[@]}, or Enter to quit) " choice
[[ $choice =~ ^[0-9]+$ ]] && ((choice >= 1 && choice <= ${#commits[@]})) || { echo "Nothing changed."; exit 0; }

commit=${commits[$((choice - 1))]}
name=${names[$((choice - 1))]}
dir=$REPO-$name
binary=$dir/build/bin/llama-server

if [[ -x $binary ]]; then
  echo "$name is already built in $dir."
else
  if [[ ! -d $dir ]]; then
    git -C "$REPO" worktree add --detach "$dir" "$commit"
  elif [[ $(git -C "$dir" rev-parse HEAD) != "$commit" ]]; then
    echo "$dir exists but holds a different commit. Remove it or pick another." >&2
    exit 1
  fi
  flags=(-S "$dir" -B "$dir/build" -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES="$CUDA_ARCH"
         -DLLAMA_CURL=OFF -DCMAKE_CUDA_COMPILER="$NVCC")
  if [[ -d $OVERLAY ]]; then
    flags+=(-DCMAKE_CUDA_FLAGS="-I$OVERLAY")
    export CUDAFLAGS="-I$OVERLAY"
  fi
  echo "Building $name with $JOBS compilers at once. This takes a while."
  echo "If it is interrupted, run this script again and pick $name: it carries on where it stopped."
  cmake "${flags[@]}"
  cmake --build "$dir/build" --target llama-server -j "$JOBS"
fi

echo
"$binary" --version 2>&1 | grep -iE "version|built" || true
"$binary" --list-devices 2>&1 | grep -A8 "Available devices" || echo "Warning: it lists no devices." >&2

echo
previous=$(readlink -f "$LINK" || true)
if [[ $previous == "$(readlink -f "$binary")" ]]; then
  echo "$LINK already points at $name."
elif ask "Point $LINK at $name?"; then
  ln -sfn "$binary" "$LINK"
  echo "Switched. To go back:  ln -sfn $previous $LINK"
  if systemctl --user is-active --quiet "$SERVICE"; then
    echo "Model servers that are already running keep the old build until LoadCoach restarts."
    echo "Restarting unloads every model and drops any requests in line."
    if ask "Restart $SERVICE now?"; then
      systemctl --user restart "$SERVICE"
      echo "Restarted."
    else
      echo "Left running. Restart later with:  systemctl --user restart $SERVICE"
    fi
  fi
else
  echo "Left as it was. To switch later:  ln -sfn $binary $LINK"
fi
