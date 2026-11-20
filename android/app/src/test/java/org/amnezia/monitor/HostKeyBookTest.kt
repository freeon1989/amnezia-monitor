package org.amnezia.monitor

import org.amnezia.monitor.data.HostKeyBook
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.nio.ByteBuffer
import java.util.Base64

class HostKeyBookTest {
    private fun blob(algo: String, fill: Int): ByteArray {
        val name = algo.toByteArray()
        val body = ByteArray(32) { fill.toByte() }
        return ByteBuffer.allocate(4 + name.size + 4 + body.size)
            .putInt(name.size).put(name).putInt(body.size).put(body).array()
    }

    private val ed1 = blob("ssh-ed25519", 1)
    private val ed2 = blob("ssh-ed25519", 2)
    private val rsa = blob("ssh-rsa", 3)
    private val host = "203.0.113.10"

    @Test
    fun entryNameMatchesOpenSsh() {
        assertEquals("203.0.113.10", HostKeyBook.entryName(host, 22))
        assertEquals("[203.0.113.10]:2222", HostKeyBook.entryName(host, 2222))
    }

    @Test
    fun firstKeyIsNewThenOk() {
        val keys = mutableMapOf<String, String>()
        assertEquals(HostKeyBook.Result.NEW, HostKeyBook.check(keys, host, ed1))
        HostKeyBook.add(keys, host, ed1)
        assertEquals(HostKeyBook.Result.OK, HostKeyBook.check(keys, host, ed1))
    }

    @Test
    fun changedKeyGivesBothFingerprints() {
        val keys = mutableMapOf<String, String>()
        HostKeyBook.add(keys, host, ed1)
        val mm = HostKeyBook.mismatch(keys, host, 22, ed2)
        assertNotNull(mm)
        assertEquals(HostKeyBook.fingerprint(ed1), mm!!.old.single().fingerprint)
        assertEquals(HostKeyBook.fingerprint(ed2), mm.new.fingerprint)
        assertEquals("ED25519", mm.new.type)
        assertFalse(mm.typeChanged)
    }

    @Test
    fun otherTypeIsTypeChangedNotSilentlyAccepted() {
        val keys = mutableMapOf<String, String>()
        HostKeyBook.add(keys, host, rsa)
        assertEquals(HostKeyBook.Result.TYPE_CHANGED, HostKeyBook.check(keys, host, ed1))
        assertTrue(HostKeyBook.mismatch(keys, host, 22, ed1)!!.typeChanged)
    }

    @Test
    fun legacyEntryWithoutTypeStillWorks() {
        val keys = mutableMapOf(host to Base64.getEncoder().encodeToString(ed1))
        assertEquals(HostKeyBook.Result.OK, HostKeyBook.check(keys, host, ed1))
        assertEquals(HostKeyBook.Result.CHANGED, HostKeyBook.check(keys, host, ed2))
    }

    @Test
    fun replaceDropsOnlyThatHost() {
        val keys = mutableMapOf(host to Base64.getEncoder().encodeToString(ed1))
        HostKeyBook.add(keys, "198.51.100.1", rsa)
        HostKeyBook.replace(keys, host, ed2)
        assertEquals(HostKeyBook.Result.OK, HostKeyBook.check(keys, host, ed2))
        assertEquals(HostKeyBook.Result.OK, HostKeyBook.check(keys, "198.51.100.1", rsa))
        assertNull(keys[host])
    }

    @Test
    fun knownTypesArePreferred() {
        val keys = mutableMapOf<String, String>()
        HostKeyBook.add(keys, host, rsa)
        val order = HostKeyBook.preferredAlgorithms(keys, host, listOf("ssh-ed25519", "ecdsa-sha2-nistp256", "rsa-sha2-512", "rsa-sha2-256"))
        assertEquals(listOf("rsa-sha2-512", "rsa-sha2-256", "ssh-ed25519", "ecdsa-sha2-nistp256"), order)
    }
}
