package org.amnezia.monitor

import org.amnezia.monitor.data.ProcParse
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class CpuParseTest {
    @Test
    fun oneCoreVpsTwoSamples() {
        val t = "cpu  1898 91 2636 74816 81 0 71 0 0 0\ncpu  1902 91 2643 74905 81 0 71 0 0 0\n"
        assertEquals(11.0, ProcParse.cpuFromSamples(t)!!, 0.001)
        assertEquals(1902L + 91 + 2643 + 74905 + 81 + 71, ProcParse.cpuJiffies(t)!!.second)
    }

    @Test
    fun stealIsBusyAndPerCpuLinesIgnored() {
        val t = "cpu  10000 0 5000 80000 100 0 50 2000 0 0\ncpu0 10000 0 5000 80000 100 0 50 2000 0 0\n" +
            "cpu  10020 0 5010 80050 100 0 50 2020 0 0\n"
        assertEquals(50.0, ProcParse.cpuFromSamples(t)!!, 0.001)
    }

    @Test
    fun oldKernelSingleSampleAndGarbage() {
        assertEquals(20.0, ProcParse.cpuFromSamples("cpu 100 0 50 1000\ncpu 110 0 60 1080\n")!!, 0.001)
        assertNull(ProcParse.cpuFromSamples("cpu  1 2 3 4 5 6 7 8 9 10\n"))
        assertNull(ProcParse.cpuFromSamples("%ЦП(ы):  0,0 us,  9,1 sy\n"))
        assertNull(ProcParse.cpuJiffies("cpu  x y z w\n"))
    }

    @Test
    fun statCommandIsWhitelisted() {
        assertTrue(ProcParse.allowed(ProcParse.STAT_CMD))
        assertTrue(ProcParse.allowed("head -1 /proc/stat"))
        assertFalse(ProcParse.allowed("head -1 /proc/stat; rm -rf /"))
    }
}
