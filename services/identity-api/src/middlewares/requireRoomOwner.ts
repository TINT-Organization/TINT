import { Request, Response, NextFunction } from 'express';
import { isOwner } from '../modules/rooms/rooms.repo.js';
import { auditDenied } from '../modules/audit/audit.events.js';

/** 403 y `access.denied` en la bitácora, con el actor y la sala. */
function forbidden(req: Request, res: Response, roomId: string | undefined) {
  void auditDenied({ user: req.user, roomId });
  return res.status(403).json({ error: 'Forbidden' });
}

export async function requireRoomOwner(req: Request, res: Response, next: NextFunction) {
  const rawId = req.params?.id;
  const roomId = Array.isArray(rawId) ? rawId[0] : rawId;
  try {
    if (!roomId || !req.user?.id) {
      return forbidden(req, res, roomId);
    }
    const owner = await isOwner(roomId, req.user.id);
    if (!owner) {
      return forbidden(req, res, roomId);
    }
    next();
  } catch (err) {
    return forbidden(req, res, roomId);
  }
}
