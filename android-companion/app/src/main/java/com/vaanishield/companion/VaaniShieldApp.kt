package com.vaanishield.companion

import android.app.Application
import androidx.room.Room
import com.vaanishield.companion.data.local.EventDatabase

class VaaniShieldApp : Application() {
    val database by lazy { Room.databaseBuilder(this, EventDatabase::class.java, "pending-events.db").build() }
}
