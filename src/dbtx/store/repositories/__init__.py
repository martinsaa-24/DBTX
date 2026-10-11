"""Repositories: the store's write paths (plan section 5).

Every function takes the caller's session (and the adapter where it needs dialect SQL),
contains no dialect branching and never commits: the caller owns the transaction.
"""
