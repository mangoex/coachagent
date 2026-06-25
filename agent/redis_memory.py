import redis
import json
import logging
from config.settings import settings
from typing import List, Dict, Any, Optional, Tuple

logger = logging.getLogger(__name__)

class RedisConversationMemory:
    """
    Manages session persistence and state machines.
    Falls back to an in-memory dictionary if Redis is unavailable (useful for local dev).
    """
    def __init__(self):
        try:
            self.redis_client = redis.from_url(settings.REDIS_URL, decode_responses=True)
            self.redis_client.ping()
            self.use_redis = True
            logger.info("Connected to Redis successfully.")
        except Exception as e:
            logger.warning(
                f"Failed to connect to Redis at {settings.REDIS_URL}: {str(e)}. "
                "Falling back to thread-unsafe local in-memory storage."
            )
            self.use_redis = False
            self._local_db = {}

    def get_history(self, phone_number: str) -> List[Dict[str, str]]:
        """
        Retrieves the conversation history. Returns list of messages: [{'role': 'user/model', 'parts': [...]}]
        """
        key = f"chat_history:{phone_number}"
        if self.use_redis:
            try:
                data = self.redis_client.get(key)
                if data:
                    return json.loads(data)
            except Exception as e:
                logger.error(f"Error reading chat history from Redis: {str(e)}")
        else:
            return self._local_db.get(key, [])
        return []

    def add_message(self, phone_number: str, role: str, content: str, ttl: int = 86400):
        """
        Appends a message to the history. Automatically enforces a context window limit (last 20 messages).
        """
        key = f"chat_history:{phone_number}"
        history = self.get_history(phone_number)
        
        # Format for Gemini: role is 'user' or 'model', content is mapped to parts
        history.append({"role": role, "content": content})
        
        # Cap window length to prevent context explosion
        if len(history) > 50:
            history = history[-50:]

        if self.use_redis:
            try:
                self.redis_client.setex(key, ttl, json.dumps(history))
            except Exception as e:
                logger.error(f"Error saving chat history to Redis: {str(e)}")
        else:
            self._local_db[key] = history

    def clear_history(self, phone_number: str):
        """
        Clears the conversation history.
        """
        key = f"chat_history:{phone_number}"
        if self.use_redis:
            try:
                self.redis_client.delete(key)
            except Exception as e:
                logger.error(f"Error clearing history in Redis: {str(e)}")
        else:
            self._local_db.pop(key, None)

    def set_state(self, phone_number: str, state: str, metadata: Optional[Dict[str, Any]] = None, ttl: int = 3600):
        """
        Sets the state machine state and metadata (e.g. current meeting being audited).
        """
        key = f"chat_state:{phone_number}"
        value = {"state": state, "metadata": metadata or {}}
        if self.use_redis:
            try:
                self.redis_client.setex(key, ttl, json.dumps(value))
            except Exception as e:
                logger.error(f"Error setting state in Redis: {str(e)}")
        else:
            self._local_db[key] = value

    def get_state(self, phone_number: str) -> Tuple[Optional[str], Dict[str, Any]]:
        """
        Gets the state machine state and metadata.
        """
        key = f"chat_state:{phone_number}"
        if self.use_redis:
            try:
                data = self.redis_client.get(key)
                if data:
                    val = json.loads(data)
                    return val.get("state"), val.get("metadata", {})
            except Exception as e:
                logger.error(f"Error getting state from Redis: {str(e)}")
        else:
            val = self._local_db.get(key)
            if val:
                return val.get("state"), val.get("metadata", {})
        return None, {}

    def clear_state(self, phone_number: str):
        """
        Clears the state machine state.
        """
        key = f"chat_state:{phone_number}"
        if self.use_redis:
            try:
                self.redis_client.delete(key)
            except Exception as e:
                logger.error(f"Error clearing state in Redis: {str(e)}")
        else:
            self._local_db.pop(key, None)

# Global singleton
redis_memory = RedisConversationMemory()
