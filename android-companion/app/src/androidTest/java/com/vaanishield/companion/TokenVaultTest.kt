package com.vaanishield.companion

import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.vaanishield.companion.security.TokenVault
import org.junit.Assert.assertEquals
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class TokenVaultTest {
    @Test fun tokenCanBeEncryptedRetrievedAndCleared() {
        val context = InstrumentationRegistry.getInstrumentation().targetContext
        val vault = TokenVault(context)
        vault.clear()
        vault.store("test-device-token")
        assertEquals("test-device-token", vault.read())
        vault.clear()
        assertEquals(null, vault.read())
    }
}
