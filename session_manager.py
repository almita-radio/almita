#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Session Manager - Manages observation sessions and allows resuming interrupted captures
"""

import csv
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, List


class SessionManager:
    """
    Manages observation sessions for resumable captures
    """
    
    def __init__(self, control_dir: str = "./data/IQ"):
        """
        Initialize session manager
        
        Args:
            control_dir: Directory for session control files (default: ./data/IQ)
        """
        self.control_dir = Path(control_dir)
        self.control_dir.mkdir(parents=True, exist_ok=True)
        
        self.session_file = self.control_dir / "session.csv"
        
        # CSV fieldnames for session tracking
        self.fieldnames = [
            'session_id',
            'session_name',
            'csv_plan_path',
            'status',  # active, paused, completed, failed
            'last_point_completed',
            'total_points',
            'points_completed',
            'points_failed',
            'start_time',
            'last_update_time',
            'device_name'
        ]
        
        # Initialize session file if doesn't exist
        if not self.session_file.exists():
            self._initialize_session_file()
    
    def _initialize_session_file(self):
        """Create session control file with headers"""
        with open(self.session_file, 'w', newline='') as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=self.fieldnames)
            writer.writeheader()
    
    def get_active_sessions(self) -> List[Dict]:
        """
        Get list of active or paused sessions
        
        Returns:
            List of session dictionaries
        """
        active_sessions = []
        
        if not self.session_file.exists():
            return active_sessions
        
        with open(self.session_file, 'r', newline='') as csvfile:
            reader = csv.DictReader(csvfile)
            for row in reader:
                if row['status'] in ['active', 'paused']:
                    active_sessions.append(row)
        
        return active_sessions
    
    def create_session(self, session_name: str, csv_plan_path: str, 
                      total_points: int, device_name: str) -> str:
        """
        Create new session entry
        
        Args:
            session_name: Name of the session
            csv_plan_path: Path to observation plan CSV
            total_points: Total number of points in plan
            device_name: Telescope device name
            
        Returns:
            session_id
        """
        session_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        start_time = datetime.now(timezone.utc).isoformat()
        
        session_data = {
            'session_id': session_id,
            'session_name': session_name,
            'csv_plan_path': csv_plan_path,
            'status': 'active',
            'last_point_completed': '0',
            'total_points': str(total_points),
            'points_completed': '0',
            'points_failed': '0',
            'start_time': start_time,
            'last_update_time': start_time,
            'device_name': device_name
        }
        
        # Append to session file
        with open(self.session_file, 'a', newline='') as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=self.fieldnames)
            writer.writerow(session_data)
        
        return session_id
    
    def update_session(self, session_id: str, **kwargs):
        """
        Update session data
        
        Args:
            session_id: Session ID to update
            **kwargs: Fields to update (status, last_point_completed, etc.)
        """
        # Read all sessions
        all_sessions = []
        with open(self.session_file, 'r', newline='') as csvfile:
            reader = csv.DictReader(csvfile)
            all_sessions = list(reader)
        
        # Update specific session
        for session in all_sessions:
            if session['session_id'] == session_id:
                for key, value in kwargs.items():
                    if key in self.fieldnames:
                        session[key] = str(value)
                session['last_update_time'] = datetime.now(timezone.utc).isoformat()
                break
        
        # Write back
        with open(self.session_file, 'w', newline='') as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=self.fieldnames)
            writer.writeheader()
            writer.writerows(all_sessions)
    
    def get_session(self, session_id: str) -> Optional[Dict]:
        """
        Get session data by ID
        
        Args:
            session_id: Session ID
            
        Returns:
            Session dictionary or None if not found
        """
        with open(self.session_file, 'r', newline='') as csvfile:
            reader = csv.DictReader(csvfile)
            for row in reader:
                if row['session_id'] == session_id:
                    return row
        return None
    
    def pause_session(self, session_id: str):
        """Mark session as paused"""
        self.update_session(session_id, status='paused')
    
    def complete_session(self, session_id: str):
        """Mark session as completed"""
        self.update_session(session_id, status='completed')
    
    def fail_session(self, session_id: str):
        """Mark session as failed"""
        self.update_session(session_id, status='failed')
