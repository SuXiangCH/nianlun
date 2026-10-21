"""Lua ownership scripts ported verbatim from the TypeScript gateway.

Every script is evaluated atomically. Keys for one bot share a hash tag so the
scripts also work on Redis Cluster.
"""

ACQUIRE_SCRIPT = """
local bindingChannel = redis.call('HGET', KEYS[4], 'channelId')
local revision = tonumber(redis.call('HGET', KEYS[4], 'revision') or '0')
local incomingRevision = tonumber(ARGV[4])
if incomingRevision <= revision and bindingChannel and bindingChannel ~= ARGV[3] then return -2 end
if incomingRevision < revision then return -3 end
if incomingRevision <= tonumber(redis.call('HGET', KEYS[4], 'expiredRevision') or '0') then return -4 end
if incomingRevision > revision then
  redis.call('HSET', KEYS[4], 'channelId', ARGV[3], 'revision', incomingRevision)
  redis.call('DEL', KEYS[3])
end
if redis.call('EXISTS', KEYS[1]) == 1 then return 0 end
if redis.call('EXISTS', KEYS[3]) == 1 then
  local revision = tonumber(redis.call('HGET', KEYS[3], 'revision'))
  if tonumber(ARGV[4]) <= revision then return -1 end
end
local generation = redis.call('INCR', KEYS[2])
redis.call('SET', KEYS[1], ARGV[1] .. ':' .. generation, 'PX', ARGV[2])
redis.call('DEL', KEYS[3])
return {generation, tonumber(redis.call('GET', KEYS[5]) or '0')}
"""

BLOCK_SCRIPT = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
if redis.call('HGET', KEYS[3], 'channelId') ~= ARGV[2] then return 0 end
if tonumber(redis.call('HGET', KEYS[3], 'revision') or '0') ~= tonumber(ARGV[3]) then return 0 end
if tonumber(ARGV[3]) <= tonumber(redis.call('HGET', KEYS[3], 'expiredRevision') or '0') then return 0 end
if tonumber(redis.call('GET', KEYS[4]) or '0') ~= tonumber(ARGV[4]) then return 0 end
redis.call('HSET', KEYS[2], 'channelId', ARGV[2], 'revision', ARGV[3])
return 1
"""

RECOVER_SCRIPT = """
if redis.call('HGET', KEYS[1], 'channelId') ~= ARGV[1] then return -1 end
if tonumber(redis.call('HGET', KEYS[1], 'revision') or '0') ~= tonumber(ARGV[3]) then return -3 end
if tonumber(ARGV[3]) <= tonumber(redis.call('HGET', KEYS[1], 'expiredRevision') or '0') then return -4 end
if redis.call('SISMEMBER', KEYS[4], ARGV[2]) == 1 then return 0 end
if redis.call('EXISTS', KEYS[2]) == 0 and redis.call('EXISTS', KEYS[5]) == 1 then return -2 end
redis.call('SADD', KEYS[4], ARGV[2])
redis.call('DEL', KEYS[2])
redis.call('INCR', KEYS[3])
return 1
"""

RENEW_SCRIPT = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
if redis.call('HGET', KEYS[2], 'channelId') ~= ARGV[3] then return 0 end
if tonumber(redis.call('HGET', KEYS[2], 'revision') or '0') ~= tonumber(ARGV[4]) then return 0 end
if tonumber(ARGV[4]) <= tonumber(redis.call('HGET', KEYS[2], 'expiredRevision') or '0') then return 0 end
if tonumber(redis.call('GET', KEYS[3]) or '0') ~= tonumber(ARGV[5]) then return 0 end
redis.call('PEXPIRE', KEYS[1], ARGV[2])
return 1
"""

RELEASE_SCRIPT = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
return redis.call('DEL', KEYS[1])
"""

PUBLISH_STATUS_SCRIPT = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return nil end
if redis.call('HGET', KEYS[2], 'channelId') ~= ARGV[2] then return nil end
if tonumber(redis.call('HGET', KEYS[2], 'revision') or '0') ~= tonumber(ARGV[3]) then return nil end
if tonumber(ARGV[3]) <= tonumber(redis.call('HGET', KEYS[2], 'expiredRevision') or '0') then return nil end
if tonumber(redis.call('GET', KEYS[3]) or '0') ~= tonumber(ARGV[4]) then return nil end
if tonumber(redis.call('GET', KEYS[4]) or '0') ~= tonumber(ARGV[5]) then return nil end
local ttl = redis.call('PTTL', KEYS[1])
if ttl <= tonumber(ARGV[6]) then return nil end
if ARGV[7] ~= 'online' and ARGV[8] == 'CHANNEL_EXPIRED' then
  redis.call('HSET', KEYS[2], 'expiredRevision', ARGV[3])
end
local value = { channelId = ARGV[2], revision = tonumber(ARGV[3]),
  status = ARGV[7] }
if ARGV[7] ~= 'online' and ARGV[8] ~= '' then value.errorCode = ARGV[8] end
local payload = cjson.encode(value)
redis.call('HSET', KEYS[5], 'token', ARGV[1], 'generation', ARGV[4],
  'recoveryVersion', ARGV[5], 'payload', payload)
return {payload, ttl}
"""

READ_STATUS_SCRIPT = """
if redis.call('HGET', KEYS[2], 'channelId') ~= ARGV[1] then return nil end
if tonumber(redis.call('HGET', KEYS[2], 'revision') or '0') ~= tonumber(ARGV[2]) then return nil end
local payload = redis.call('HGET', KEYS[5], 'payload')
if not payload then return nil end
if tonumber(redis.call('HGET', KEYS[5], 'generation') or '0') ~= tonumber(redis.call('GET', KEYS[3]) or '0') then return nil end
if tonumber(redis.call('HGET', KEYS[5], 'recoveryVersion') or '0') ~= tonumber(redis.call('GET', KEYS[4]) or '0') then return nil end
local value = cjson.decode(payload)
if value.channelId ~= ARGV[1] or value.revision ~= tonumber(ARGV[2]) then return nil end
if value.status ~= 'online' and (value.errorCode == 'CHANNEL_EXPIRED' or
  value.errorCode == 'CONNECTION_CONFLICT' or value.errorCode == 'GATEWAY_STOPPED') then
  return {payload, -1}
end
if redis.call('GET', KEYS[1]) ~= redis.call('HGET', KEYS[5], 'token') then return nil end
local ttl = redis.call('PTTL', KEYS[1])
if ttl <= tonumber(ARGV[3]) then return nil end
return {payload, ttl}
"""
