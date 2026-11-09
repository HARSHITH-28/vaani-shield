package com.vaanishield.companion.data.local

import androidx.room.*

@Entity(tableName = "queued_events")
data class QueuedEvent(@PrimaryKey val dedupKey: String, val sender: String, val senderType: String, val text: String, val timestamp: String, val sourcePackage: String, val queuedAt: Long = System.currentTimeMillis())

@Dao interface EventDao {
    @Insert(onConflict = OnConflictStrategy.IGNORE) suspend fun enqueue(event: QueuedEvent)
    @Query("SELECT * FROM queued_events ORDER BY queuedAt ASC LIMIT 1") suspend fun oldest(): QueuedEvent?
    @Query("SELECT COUNT(*) FROM queued_events") suspend fun count(): Int
    @Query("DELETE FROM queued_events WHERE dedupKey = :key") suspend fun remove(key: String)
    @Query("SELECT dedupKey FROM queued_events ORDER BY queuedAt DESC LIMIT 500") suspend fun recentKeys(): List<String>
}

@Database(entities = [QueuedEvent::class], version = 1, exportSchema = false)
abstract class EventDatabase : RoomDatabase() { abstract fun events(): EventDao }
