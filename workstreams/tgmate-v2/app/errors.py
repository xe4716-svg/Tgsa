class DeferredIngress(Exception):
 """A referenced outbound message is still in flight; retry the same update later."""
