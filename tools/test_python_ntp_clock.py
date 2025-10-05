"""Test Python NTP Clock"""
import time
from portacode.utils.ntp_clock import ntp_clock

print("=" * 50)
print("Testing Python NTP Clock")
print("=" * 50)

# Wait for initial sync
print("\nWaiting for initial sync...")
time.sleep(2)

# Print status
status = ntp_clock.get_status()
print(f"\nSync Status: {'✅ SYNCED' if status['is_synced'] else '❌ NOT SYNCED'}")
print(f"Server: {status['server']}")
print(f"Offset: {status['offset_ms']}ms" if status['offset_ms'] is not None else "Offset: None")
print(f"Last Sync: {status['last_sync']}")

# Compare timestamps
print("\nTimestamp Comparison:")
print(f"  Local time (ms):  {int(time.time() * 1000)}")
ntp_time = ntp_clock.now_ms()
print(f"  NTP time (ms):    {ntp_time if ntp_time is not None else 'None (not synced)'}")
ntp_iso = ntp_clock.now_iso()
print(f"  NTP time (ISO):   {ntp_iso if ntp_iso is not None else 'None (not synced)'}")

# Test multiple calls
print("\nTesting consistency (10 calls):")
for i in range(10):
    ts = ntp_clock.now_ms()
    if ts is not None:
        print(f"  {i+1}: {ts}")
    else:
        print(f"  {i+1}: None (not synced)")
    time.sleep(0.1)

print("\n✅ Test complete")
print("=" * 50)
