# route-rows.awk -- pick the rpmdb rows one collection stream must fetch.
#
# Usage: awk -v major=<N> [-v streams="9 10"] -f route-rows.awk rpmdb/*.tsv
#
# Prints "<ocp-minor>\t<row>" for every row of every tsv that belongs to
# stream <major>, where <row> is the tsv's own name|epoch|ver|rel|arch line.
#
# A row belongs to the stream its package was BUILT for, read off the dist tag
# in the release field (".el9", ".el9_6", ".rhaos4.21.el9" -> 9). The tsv's
# file name (<patch>[-elN][-extensions].tsv) only says which node OS SHIPS the
# package, and the two disagree: 4.21.42-el10-extensions.tsv lists
# kata-containers-3.25.0-7.rhaos4.21.el9, an el9 build that RHCOS 10 installs
# as-is. Routed by file name it went to the el10 pass, whose UBI10 container
# can only see RHEL 10 repos and never found it (2026-11-10, fetched by hand).
#
# The file name is still the fallback: for a row with no dist tag, and for a
# tag naming a stream the pipeline does not run (`streams`), which would
# otherwise be routed to a pass that never happens and silently dropped.
#
# <ocp-minor> comes from the file name (4.21.42-el10-extensions -> 4.21) and
# tells the caller which rhocp-<minor> repos to enable, so a routed-in row
# brings its own minor along.
BEGIN {
    FS = OFS = "\t"
    if (streams == "") streams = "9 10"
    n = split(streams, s, " ")
    for (i = 1; i <= n; i++) known[s[i]] = 1
    moved = 0
}
FNR == 1 {
    b = FILENAME
    sub(/.*\//, "", b); sub(/\.tsv$/, "", b); sub(/-extensions$/, "", b)
    fstream = "9"
    if (match(b, /-el[0-9]+$/)) {
        fstream = substr(b, RSTART + 3)
        b = substr(b, 1, RSTART - 1)
    }
    minor = b
    if (match(minor, /^[0-9]+\.[0-9]+/)) minor = substr(minor, 1, RLENGTH)
}
NF >= 4 {
    st = fstream
    if (match($4, /\.el[0-9]+/)) {
        t = substr($4, RSTART + 3, RLENGTH - 3)
        if (t in known) st = t
    }
    if (st != major) next
    if (st != fstream) moved++
    print minor, $0
}
END {
    if (moved) printf "%d row(s) routed in from other streams' tsvs by dist tag\n", moved > "/dev/stderr"
}
