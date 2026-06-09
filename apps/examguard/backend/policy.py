def resolve_student_session(token, claimed_sid, sessions):
    sid = sessions.get(token or '')
    if not sid or (claimed_sid and claimed_sid != sid):
        return None
    return sid


def record_coding_vote(votes, suspicious):
    votes.append(bool(suspicious))
    return (
        bool(suspicious)
        and len(votes) >= 3
        and sum(1 for vote in votes if vote) >= 2
    )
