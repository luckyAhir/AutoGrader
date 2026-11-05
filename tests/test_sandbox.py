"""The sandbox must survive crashing, hanging and flooding programs (needs a C compiler)."""

import unittest

from autograder.sandbox import Limits, Sandbox

from .helpers import TempDirCase, toolchain_or_skip

LIMITS = Limits(cpu_time_s=1.0, wall_time_s=3.0, memory_mb=64, output_bytes=64 * 1024)


class SandboxTest(TempDirCase):
    @classmethod
    def setUpClass(cls):
        cls.toolchain = toolchain_or_skip()

    def run_c(self, code: str, stdin: str = "", limits: Limits = LIMITS):
        sandbox = Sandbox(self.toolchain, self.tmp / "scratch")
        src = self.write_c("prog.c", code)
        res = sandbox.compile([src.name], "prog" + self.toolchain.exe_suffix, "-O0 -std=c11", "-lm", cwd=self.tmp)
        self.assertTrue(res.ok, res.output)
        return sandbox.run([str(res.exe)], stdin, limits)

    def test_ok(self):
        r = self.run_c('#include <stdio.h>\nint main(void){int a,b; if(scanf("%d %d",&a,&b)!=2) return 1;'
                       ' printf("%d\\n",a+b); return 0;}', "7 5\n")
        self.assertEqual((r.status, r.stdout), ("OK", "12\n"))

    def test_infinite_loop_is_tle(self):
        r = self.run_c("int main(void){ volatile unsigned long x = 0; for(;;) x++; }")
        self.assertEqual(r.status, "TLE")

    def test_sleeping_program_hits_wall_clock(self):
        # uses no CPU at all, so only the wall-clock safety net can stop it
        r = self.run_c("#include <unistd.h>\nint main(void){ sleep(30); return 0; }",
                       limits=Limits(cpu_time_s=1.0, wall_time_s=1.5, memory_mb=64))
        self.assertEqual(r.status, "TLE")
        self.assertLess(r.wall_time_s, 10)

    def test_segfault_is_runtime_error(self):
        r = self.run_c("int main(void){ volatile int *p = 0; *p = 1; return 0; }")
        self.assertEqual(r.status, "RE")
        self.assertEqual(r.signal_name, "SIGSEGV")

    def test_division_by_zero(self):
        r = self.run_c("int main(void){ volatile int z = 0; return 10 / z; }")
        self.assertEqual(r.status, "RE")
        self.assertEqual(r.signal_name, "SIGFPE")

    def test_nonzero_exit(self):
        r = self.run_c("int main(void){ return 3; }")
        self.assertEqual((r.status, r.exit_code, r.signal_name), ("RE", 3, None))

    def test_output_flood_is_ole(self):
        r = self.run_c('#include <stdio.h>\nint main(void){ for(;;) puts("spam spam spam"); }')
        self.assertEqual(r.status, "OLE")

    def test_memory_hog_is_stopped(self):
        r = self.run_c("#include <stdlib.h>\n#include <string.h>\nint main(void){ for(;;){ char *p = malloc(1<<20);"
                       " if(!p) return 7; memset(p, 1, 1<<20);} }")
        self.assertIn(r.status, ("MLE", "RE"))


if __name__ == "__main__":
    unittest.main()
