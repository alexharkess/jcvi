"""
Codes to submit multiple jobs to JCVI grid engine
"""

import os.path as op
import sys
import re
import logging

from multiprocessing import Process, Queue, cpu_count

from jcvi.formats.base import write_file, must_open
from jcvi.apps.base import OptionParser, ActionDispatcher, popen, backup, \
            mkdir, sh, listify


class Dependency (object):
    """
    Used by MakeManager.
    """
    def __init__(self, source, target, cmds, remove=False):
        self.source = listify(source)
        self.target = listify(target)
        self.cmds = listify(cmds)
        if remove:
            rm_cmd = "rm -f {0}".format(" ".join(self.target))
            self.cmds = [rm_cmd] + self.cmds

    def __str__(self):
        source = " ".join(self.source)
        target = " ".join(self.target)
        s = "{0} : {1}\n".format(target, source)
        for c in self.cmds:
            s += "\t" + c + "\n"
        return s


class MakeManager (list):
    """
    Write and execute makefile.
    """
    def __init__(self, filename="makefile"):
        self.makefile = filename
        self.targets = []

    def add(self, source, target, cmds, remove=False):
        d = Dependency(source, target, cmds, remove=remove)
        self.append(d)
        self.targets.append(target)

    def write(self):
        assert self.targets, "No targets specified"
        filename = self.makefile
        if op.exists(filename):
            backup(filename)
        fw = open(filename, "w")
        print >> fw, "all : {0}\n".format(" ".join(self.targets))
        for d in self:
            print >> fw, d
        fw.close()
        logging.debug("Makefile written to `{0}`.".format(self.makefile))

    def run(self, cpus=1):
        cmd = "make -j {0} -f {1}".format(cpus, self.makefile)
        sh(cmd)


class Jobs (list):
    """
    Runs multiple funcion calls on the SAME computer, using multiprocessing.
    """
    def __init__(self, target, args):

        for x in args:
            self.append(Process(target=target, args=x))

    def start(self):
        for pi in self:
            pi.start()

    def join(self):
        for pi in self:
            pi.join()

    def run(self):
        self.start()
        self.join()


class Poison:
    pass


class WriteJobs (object):
    """
    Runs multiple function calls, but write to the same file.

    Producer-consumer model.
    """
    def __init__(self, target, args, filename, cpus=cpu_count()):
        workerq = Queue()
        writerq = Queue()

        for a in args:
            workerq.put(a)

        cpus = min(cpus, len(a))
        for i in xrange(cpus):
            workerq.put(Poison())

        self.worker = Jobs(work, args=[(workerq, writerq, target)] * cpus)
        self.writer = Process(target=write, args=(workerq, writerq, \
                                                  filename, cpus))

    def run(self):
        self.worker.start()
        self.writer.start()
        self.worker.join()
        self.writer.join()


def work(queue_in, queue_out, target):
    while True:
        a = queue_in.get()
        if isinstance(a, Poison):
            break
        res = target(a)
        queue_out.put(res)
    queue_out.put(Poison())


def write(queue_in, queue_out, filename, cpus):
    from jcvi.utils.progressbar import ProgressBar, Percentage, Bar, ETA

    fw = must_open(filename, "w")
    isize = queue_in.qsize()
    logging.debug("A total of {0} items to compute.".format(isize))
    isize = isize or 1
    widgets = ['Queue: ', Percentage(), ' ',
               Bar(marker='>', left='[', right=']'), ' ', ETA()]
    p = ProgressBar(maxval=isize, term_width=60, widgets=widgets).start()
    poisons = 0
    while True:
        res = queue_out.get()
        qsize = queue_in.qsize()
        p.update(isize - qsize)
        if isinstance(res, Poison):
            poisons += 1
            if poisons == cpus:  # wait all workers finish
                break
        elif res:
            print >> fw, res
            fw.flush()
    fw.close()


class GridOpts (dict):

    def __init__(self, opts):
        export = ("pcode", "queue", "threaded", "concurrency",
                  "outdir", "name", "hold_jid")
        for e in export:
            if e in opts.__dict__:
                self[e] = getattr(opts, e)


class GridProcess (object):

    pat1 = re.compile(r"Your job (?P<id>[0-9]*) ")
    pat2 = re.compile(r"Your job-array (?P<id>\S*) ")

    def __init__(self, cmd, jobid="", pcode="0372", queue="default", threaded=None,
                       infile=None, outfile=None, errfile=None, arr=None,
                       concurrency=None, outdir=".", name=None, hold_jid=None,
                       grid_opts=None):

        self.cmd = cmd
        self.jobid = jobid
        self.queue = queue
        self.threaded = threaded
        self.infile = infile
        self.outfile = outfile or ""
        self.errfile = errfile or ""
        self.arr = arr
        self.concurrency = concurrency
        self.outdir = outdir
        self.name = name
        self.pcode = pcode
        self.hold_jid = hold_jid
        self.pat = self.pat2 if arr else self.pat1
        if grid_opts:
            self.__dict__.update(GridOpts(grid_opts))

    def __str__(self):
        return "\t".join((x for x in \
                (self.jobid, self.cmd, self.outfile) if x))

    def build(self):
        # Shell commands
        if "|" in self.cmd or "&&" in self.cmd or "||" in self.cmd:
            quote = "\"" if "'" in self.cmd else "'"
            self.cmd = "sh -c {1}{0}{1}".format(self.cmd, quote)

        # qsub command (the project code is specific to jcvi)
        qsub = "qsub -P {0} -cwd".format(self.pcode)
        if self.queue != "default":
            qsub += " -l {0}".format(self.queue)
        if self.threaded:
            qsub += " -pe threaded {0}".format(self.threaded)
        if self.arr:
            assert 1 <= self.arr < 100000
            qsub += " -t 1-{0}".format(self.arr)
        if self.concurrency:
            qsub += " -tc {0}".format(self.concurrency)
        if self.name:
            qsub += ' -N "{0}"'.format(self.name)
        if self.hold_jid:
            param = "-hold_jid_ad" if self.arr else "-hold_jid"
            qsub += " {0} {1}".format(param, self.hold_jid)

        # I/O
        infile = self.infile
        outfile = self.outfile
        errfile = self.errfile
        outdir = self.outdir
        mkdir(outdir)
        redirect_same = outfile and (outfile == errfile)

        if infile:
            qsub += " -i {0}".format(infile)
        if outfile:
            self.outfile = op.join(outdir, outfile)
            qsub += " -o {0}".format(self.outfile)
        if errfile:
            if redirect_same:
                qsub += " -j y"
            else:
                self.errfile = op.join(outdir, errfile)
                qsub += " -e {0}".format(self.errfile)

        cmd = " ".join((qsub, self.cmd))
        return cmd

    def start(self):
        cmd = self.build()
        # run the command and get the job-ID (important)
        output = popen(cmd, debug=False).read()

        if output.strip() != "":
            self.jobid = re.search(self.pat, output).group("id")
        else:
            self.jobid = "-1"

        msg = "[{0}] {1}".format(self.jobid, self.cmd)
        if self.infile:
            msg += " < {0} ".format(self.infile)
        if self.outfile:
            backup(self.outfile)
            msg += " > {0} ".format(self.outfile)
        if self.errfile:
            backup(self.errfile)
            msg += " 2> {0} ".format(self.errfile)

        logging.debug(msg)


class Grid (list):

    def __init__(self, cmds, outfiles=[]):

        assert cmds, "Commands empty!"
        if not outfiles:
            outfiles = [None] * len(cmds)

        for cmd, outfile in zip(cmds, outfiles):
            self.append(GridProcess(cmd, outfile=outfile))

    def run(self):
        for pi in self:
            pi.start()


arraysh = """
CMD=`awk "NR==$SGE_TASK_ID" {0}`
$CMD"""


def get_grid_engine():
    cmd = "qsub --version"
    popen(cmd, debug=False).read()
    return "PBS" if "PBS" in cmd else "SGE"


def main():

    actions = (
        ('run', 'run a normal command on grid'),
        ('array', 'run an array job'),
        ('kill', 'wrapper around the `qdel` command'),
            )

    p = ActionDispatcher(actions)
    p.dispatch(globals())


def array(args):
    """
    %prog array commands.list

    Parallelize a set of commands on grid using array jobs.
    """
    p = OptionParser(array.__doc__)
    p.set_grid_opts(array=True)
    opts, args = p.parse_args(args)

    if len(args) != 1:
        sys.exit(not p.print_help())

    cmds, = args
    fp = open(cmds)
    ncmds = sum(1 for x in fp)
    fp.close()

    pf = cmds.rsplit(".",  1)[0]
    runfile = pf + ".sh"
    assert runfile != cmds, \
            "Commands list file should not have a `.sh` extension"

    contents = arraysh.format(cmds)
    write_file(runfile, contents)

    outfile = "{0}.{1}.out".format(pf, "\$TASK_ID")
    p = GridProcess("sh {0}".format(runfile), outfile=outfile, errfile=outfile,
                    arr=ncmds, grid_opts=opts)
    p.start()


def run(args):
    """
    %prog run command ::: file1 file2

    Parallelize a set of commands on grid. The syntax is modeled after GNU
    parallel <http://www.gnu.org/s/parallel/man.html#options>

    {}   - input line
    {.}  - input line without extension
    {_}  - input line first part
    {/}  - basename of input line
    {/.} - basename of input line without extension
    {/_} - basename of input line first part
    {#}  - sequence number of job to run
    :::  - Use arguments from the command line as input source instead of stdin
    (standard input).

    If file name is `t/example.tar.gz`, then,
    {} is "t/example.tar.gz", {.} is "t/example.tar", {_} is "t/example"
    {/} is "example.tar.gz", {/.} is "example.tar", {/_} is "example"

    A few examples:
    ls -1 *.fastq | %prog run process {} {.}.pdf  # use stdin
    %prog run process {} {.}.pdf ::: *fastq  # use :::
    %prog run "zcat {} > {.}" ::: *.gz  # quote redirection
    %prog run < commands.list  # run a list of commands
    """
    p = OptionParser(run.__doc__)
    p.set_grid_opts()
    opts, args = p.parse_args(args)

    if len(args) == 0:
        sys.exit(not p.print_help())

    sep = ":::"
    if sep in args:
        sepidx = args.index(sep)
        filenames = args[sepidx + 1:]
        args = args[:sepidx]
        if not filenames:
            filenames = [""]
    else:
        filenames = sys.stdin if not sys.stdin.isatty() else [""]

    cmd = " ".join(args)

    cmds = [] if filenames else [(cmd, None)]
    for i, filename in enumerate(filenames):
        filename = filename.strip()
        noextname = filename.rsplit(".", 1)[0]
        prefix, basename = op.split(filename)
        basenoextname = basename.rsplit(".", 1)[0]
        basefirstname = basename.split(".")[0]
        firstname = op.join(prefix, basefirstname)
        ncmd = cmd

        if "{" in ncmd:
            ncmd = ncmd.replace("{}", filename)
        else:
            ncmd += " " + filename

        ncmd = ncmd.replace("{.}", noextname)
        ncmd = ncmd.replace("{_}", firstname)
        ncmd = ncmd.replace("{/}", basename)
        ncmd = ncmd.replace("{/.}", basenoextname)
        ncmd = ncmd.replace("{/_}", basefirstname)
        ncmd = ncmd.replace("{#}", str(i))

        outfile = None
        if ">" in ncmd:
            ncmd, outfile = ncmd.split(">", 1)
            ncmd, outfile = ncmd.strip(), outfile.strip()

        ncmd = ncmd.strip()
        cmds.append((ncmd, outfile))

    for ncmd, outfile in cmds:
        p = GridProcess(ncmd, outfile=outfile, grid_opts=opts)
        p.start()


def guess_method(tag):
    from jcvi.formats.base import is_number

    jobids = tag.split(",")
    for jobid in jobids:
        if not is_number(jobid):
            return "pattern"
    return "jobid"


def kill(args):
    """
    %prog kill [options] JOBNAMEPAT/JOBIDs

    Kill jobs based on JOBNAME pattern matching (case-sensitive)
    or list of JOBIDs (comma separated)

    Examples:
    %prog kill "pyth*"                 # Use regex
    %prog kill 160253,160245,160252    # Use list of job ids
    %prog kill all                     # Everything
    """
    import shlex
    from jcvi.apps.base import sh, getusername
    from subprocess import check_output, CalledProcessError
    import xml.etree.ElementTree as ET

    valid_methods = ("pattern", "jobid")
    p = OptionParser(kill.__doc__)
    p.add_option("--method", choices=valid_methods,
                 help="Identify jobs based on [default: guess]")
    opts, args = p.parse_args(args)

    if len(args) != 1:
        sys.exit(not p.print_help())

    username = getusername()
    tag, = args
    tag = tag.strip()

    if tag == "all":
        sh("qdel -u {0}".format(username))
        return

    valid_jobids = set()
    method = opts.method or guess_method(tag)
    if method == "jobid":
        jobids = tag.split(",")
        valid_jobids |= set(jobids)
    elif method == "pattern":
        qsxmlcmd = 'qstat -u "{0}" -j "{1}" -nenv -njd -xml'.\
                                format(username, tag)
        try:
            qsxml = check_output(shlex.split(qsxmlcmd)).strip()
        except CalledProcessError, e:
            qsxml = None
            logging.debug('No jobs matching the pattern "{0}"'.format(tag))

        if qsxml is not None:
            for job in ET.fromstring(qsxml).findall("djob_info"):
                for elem in job.findall("element"):
                    jobid = elem.find("JB_job_number").text
                    valid_jobids.add(jobid)

    if valid_jobids:
        sh("qdel {0}".format(",".join(valid_jobids)))


if __name__ == '__main__':
    main()
