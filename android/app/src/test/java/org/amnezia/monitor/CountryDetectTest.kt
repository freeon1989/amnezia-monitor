package org.amnezia.monitor

import org.amnezia.monitor.data.CountryDetect
import org.amnezia.monitor.data.ThemeData
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** Те же случаи, что tests/test_theme.py на ПК: телефон и панель определяют страну одинаково. */
class CountryDetectTest {
    @Test
    fun byName() {
        val cases = mapOf(
            "Svecia" to "se", "Вена-2" to "at", "Kazahi" to "kz", "Питер" to "ru", "Frankfurt-1" to "de",
            "Германия-1" to "de", "Amsterdam" to "nl", "London" to "gb", "UK-2" to "gb", "Варшава" to "pl",
            "Paris" to "fr", "Helsinki" to "fi", "Нью-Дели" to "in", "Тель-Авив" to "il", "Гонконг" to "hk",
            "Charlotte" to "us", "USA-1" to "us", "СПБ" to "ru",
            "Ukraine" to null, "Неделя-тест" to null, "Swedbank" to null, "test1" to null, "" to null,
        )
        for ((name, cc) in cases) assertEquals(name, cc, CountryDetect.byName(name))
    }

    @Test
    fun priority() {
        assertEquals("fi" to "manual", CountryDetect.detect("Frankfurt", manual = "FI"))
        assertEquals("de" to "name", CountryDetect.detect("Frankfurt"))
        assertEquals("nl" to "geoip", CountryDetect.detect("test1", geoip = "nl"))
        assertEquals("de" to "name", CountryDetect.detect("Frankfurt", geoip = "nl"))
        assertEquals("" to "none", CountryDetect.detect("test1"))
        assertEquals("" to "none", CountryDetect.detect("test1", manual = "Germany"))
    }

    @Test
    fun themeFallbacks() {
        val se = CountryDetect.themeFor("Svecia")
        assertEquals("se", se.key)
        assertEquals("Швеция", se.countryName)
        assertEquals(0xFF1E8FE0, se.accent)
        val es = CountryDetect.themeFor("x", manual = "es")       // флаг есть, своей героини нет
        assertEquals("default", es.key)
        assertEquals("es", es.country)
        assertEquals("Испания", es.countryName)
        val none = CountryDetect.themeFor("test1")
        assertEquals("default", none.key)
        assertEquals("none", none.source)
        assertEquals("", none.country)
        assertEquals("ZZ", CountryDetect.countryName("zz"))
    }

    @Test
    fun themeDataComplete() {
        assertEquals(15, ThemeData.THEMES.size)
        assertTrue(ThemeData.THEMES.containsKey("default"))
        val choices = CountryDetect.choices()
        assertEquals(choices.size, choices.map { it.first }.toSet().size)
        assertEquals(choices.size, ThemeData.THEMES.size - 1 + ThemeData.COUNTRY_NAMES.size)
    }

    @Test
    fun geoipOnlyForPublicIpv4() {
        assertTrue(CountryDetect.isPublicIpv4("8.8.8.8"))
        for (h in listOf("10.0.0.1", "192.168.1.5", "172.16.0.1", "127.0.0.1", "100.64.1.1", "169.254.1.1",
            "203.0.113.11", "198.51.100.10", "192.0.2.1", "vpn.example.com", "", "1.2.3", "1.2.3.400", "::1")) {
            assertFalse(h, CountryDetect.isPublicIpv4(h))
        }
    }

    @Test
    fun normalize() {
        assertEquals("de", CountryDetect.normalize(" DE "))
        assertEquals("", CountryDetect.normalize("Germany"))
        assertNull(CountryDetect.byName(null))
    }
}
