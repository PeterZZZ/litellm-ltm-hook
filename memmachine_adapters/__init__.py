"""Frontend adapters for memmachine_hook: how one frontend's traffic becomes turns.

The core (memmachine_hook) owns everything shared: who the user is, the Platform
client, search, the memory block, storing, retries, dead letters and tracing. An
adapter only decides which requests are its own and what a turn is for its
frontend. It subclasses memmachine_hook.Adapter:

  name                        the frontend (X-MemMachine-Client, the stored agent_id)
  claims(call_type, headers)  True if the request is this frontend's
  pre_call(hook, key, data, headers)        before the model call: start a turn
                              (hook.new_turn, hook.start_search) and hook.inject
  on_success(hook, kwargs, response, headers)  after it: set turn.answer, hook._flush
  on_failure(hook, kwargs, headers)

The first adapter in memmachine_hook.ADAPTERS whose claims() is True handles a
request; none = no memory. Add a frontend: a module here and an entry there.
"""
