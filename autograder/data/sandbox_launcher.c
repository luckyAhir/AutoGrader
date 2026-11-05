/*
 * sandbox_launcher.c - runs one student program under resource limits.
 *
 * usage: sandbox_launcher CPU_SECONDS ADDRESS_SPACE_BYTES FILE_SIZE_BYTES STATUS_FILE -- program [args...]
 *        a limit of 0 means "do not set this limit".
 *
 * The launcher forks; the child applies setrlimit() and exec()s the program,
 * the parent waits for it with wait4() and writes one line to STATUS_FILE:
 *
 *     <signaled 0|1> <exit code or signal number> <max RSS in KB> <user s> <system s>
 *
 * Measuring in this small process (instead of in the Python grader) matters:
 * a child forked from Python inherits Python's resident memory, which would
 * show up in ru_maxrss and be mistaken for the student's memory use. Doing
 * the setrlimit() calls here also avoids Python's preexec_fn, which is unsafe
 * while analyzers run in parallel threads.
 */
#include <errno.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/resource.h>
#include <sys/time.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

static void set_limit(int resource, unsigned long long soft, unsigned long long hard)
{
    struct rlimit rl;
    rl.rlim_cur = (rlim_t)soft;
    rl.rlim_max = (rlim_t)hard;
    (void)setrlimit(resource, &rl); /* lowering a limit only fails if it is already lower */
}

int main(int argc, char **argv)
{
    if (argc < 7 || strcmp(argv[5], "--") != 0) {
        fprintf(stderr, "sandbox_launcher: usage: CPU AS FSIZE STATUS_FILE -- program [args]\n");
        return 125;
    }
    unsigned long long cpu = strtoull(argv[1], NULL, 10);
    unsigned long long as = strtoull(argv[2], NULL, 10);
    unsigned long long fsize = strtoull(argv[3], NULL, 10);
    const char *status_file = argv[4];

    set_limit(RLIMIT_CORE, 0, 0);
    pid_t pid = fork();
    if (pid < 0) {
        fprintf(stderr, "sandbox_launcher: fork failed: %s\n", strerror(errno));
        return 125;
    }
    if (pid == 0) {
        if (cpu > 0)
            set_limit(RLIMIT_CPU, cpu, cpu + 1); /* soft -> SIGXCPU, hard -> SIGKILL */
        if (as > 0) {
            set_limit(RLIMIT_AS, as, as);
            set_limit(RLIMIT_STACK, as, as); /* recursion may use up to the memory limit */
        }
        if (fsize > 0)
            set_limit(RLIMIT_FSIZE, fsize, fsize);
        execv(argv[6], argv + 6);
        fprintf(stderr, "sandbox_launcher: cannot execute %s: %s\n", argv[6], strerror(errno));
        _exit(126);
    }

    int status = 0;
    struct rusage ru;
    memset(&ru, 0, sizeof ru);
    while (wait4(pid, &status, 0, &ru) < 0) {
        if (errno != EINTR) {
            fprintf(stderr, "sandbox_launcher: wait4 failed: %s\n", strerror(errno));
            return 125;
        }
    }
    FILE *out = fopen(status_file, "w");
    if (out != NULL) {
        int signaled = WIFSIGNALED(status) ? 1 : 0;
        int value = signaled ? WTERMSIG(status) : WEXITSTATUS(status);
        fprintf(out, "%d %d %ld %ld.%06ld %ld.%06ld\n", signaled, value, (long)ru.ru_maxrss,
                (long)ru.ru_utime.tv_sec, (long)ru.ru_utime.tv_usec,
                (long)ru.ru_stime.tv_sec, (long)ru.ru_stime.tv_usec);
        fclose(out);
    }
    return 0;
}
