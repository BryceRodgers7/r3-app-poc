**Operator & Referee Window requirements**

There are 2 windows in this app: an Operator window and a Referee window. Both windows show a grid of video feeds but the Operator window is locked in to the live feed, while the Referee window can replay/rewind/slow down/speed up/pause/resume/jump to live feed. 

Operator (technician) Screen 

* Purpose \- this window allows an operator to view the live feeds, and control the creation of games & clips by pressing UI buttons.   
  * The window is dominated by a  “video panel”  
  * The video panel, by default, shows all the video feeds.  
  * Provide a control area where user can select to view/hide a video playback portal (for each NDI camera feed) on the video board.  
  * Display a ribbon of buttons, one for each video feed.  
    * When a button is clicked its video feed is hidden/restored on the video panel.  
    * The video panel should be responsive to maximize the size of the remaining video feeds.   
  * Allow user to “Start/End Game” with a button.  
    * When “End Game” is pressed, provide user confirmation “Are you sure you want to end this game?”  
    * The button is a toggle.  Green to start the game.  Red button to end the game while the game is in progress.  
  * Going forward, the concept of plays will now be clips.  
  * Display a Clip counter in the top-right. Starting at 0 when they click Start Game (0-indexed).  
  * Clips can be of multiple types.   
    * Pre-game  
      * This is the clip created when the user clicks Start Game. Each game will have exactly one clip of this type.  
    * Play  
      * Display a Play Counter and display on the screen the current play number.  
      * Each time “Next Play” is clicked, start the next play.  
      * The first Play will be number 1 (1-indexed).  
    * Timeout  
      * When the timeout button is clicked this marks the beginning of a timeout clip.  
      * During a timeout, the Play counter continues to display the latest Play number.  
    * Challenge  
      * When the Challenge button is clicked this marks the beginning of a challenge clip.  
      * During a challenge, the Play counter continues to display the latest Play number.  
      * Causes the video of the most recent play to be shown on the Referee Review window. The video clips will start at the beginning of the play and all video playbacks are paused.  
      * The challenge button cannot be pressed twice in a row, there cannot be 2 challenge clips back-to-back. As long as the current clip is a ‘challenge’, ignore subsequent presses of the ‘challenge’ button. The operator can start a new clip by pressing either ‘Next Play’ or ‘Timeout’ buttons.  
  * Clips can be “marked” by pressing the “Mark Play” button.  This can occur for any clip type.  Mark is just a database flag, it will be used later downstream by processes that are currently out-of-scope.  
  * Clips is a table in the database.  
  * When End Game is clicked  
    * Provide a modal popup “Are you sure you want to end this game?”

    

* Provide a link for “Post-process & Exit”.  
  * When clicked: stop recording (if not already stopped), stop the live feeds.    
  * Also, display a modal and process the MKV files into combined MP4 files. Show a progress bar.  Close the modal and both windows when all MP4 files are successfully created.

Referee Review Screen

* Purpose – this window allows the referee to replay/rewind/speed up/slow down/pause/resume/jump to live feed.   
  * The window is dominated by a  “video panel”  
  * The video panel, by default, shows all the video feeds.  
  * Provide a control area where user can select to view/hide a video playback portal (for each NDI camera feed) on the video board.  
  * Display a ribbon of buttons, one for each video feed.  
    * When a button is clicked its video feed is hidden/restored on the video panel.  
    * The video panel should be responsive to maximize the size of the remaining video feeds.   
* When the ‘challenge’ button on the operator screen is pressed, the video feeds on the referee window will jump to the beginning of the last play, and will be paused there.
  * During a challenge, the video feeds will show the last play. When the end of play-clip is reached, it will automatically pause, the feeds cannot go beyond that point until the challenge is over.
  * During a challenge, the video feed cannot rewind to any point earlier than the start of the most recent play (the challenged play). If the referee tries to rewind back further, it will automatically pause at the start of the play.
  * The challenge is over when the operator presses a button to start a new clip (such as ‘Next Play’ or ‘Time Out’).
  * When the challenge ends, the referee window automatically returns to the live feed — no manual jump-to-live is needed.
* Surface the current play number in the referee window's control area — **not** drawn over the video. (Nothing overlays the video feeds; see the cross-window rule below.) It shows the number of the current play, OR the last play number if the current clip is not a play, OR ‘N/A’ during the ‘pre-game’ clip. In the current build this appears on the camera-feed ribbon's selector label (and on the diagnostic status bar when diagnostics are enabled).
* There will be buttons for pause/play, 2x speed, ½ speed, ¼ speed, ⅛ speed, rewind 5 seconds, step backwards 1 frame, and step forward 1 frame.
* There will also be an inertia wheel which allows the referee to seek the video within the current clip. Whenever this wheel turns 1 degree, the video feed should seek 1 frame in that direction (forward or backward). Mouse-drag (click-and-drag in a circle around the wheel) is the only input. On release after a fast spin, the wheel coasts forward with momentum and the seek follows, decaying smoothly to a stop. A direction guide is drawn on the wheel face — curved arrows with “FF” / “REW” labels — so the rewind / fast-forward directions are obvious (counter-clockwise = forward, clockwise = rewind).
* Auto-pause / resume behavior is configurable (`[replay]` settings; see `app_settings.toml.example`):
  * **Jog wheel** — by default, when the referee touches the wheel and lets it settle (release + inertia stop), playback **resumes at whatever rate it was at before the touch** (slow-mo is preserved; if it was paused, it stays paused). `jog_wheel_resume_after_release = false` keeps it paused after settling.
  * **Step backward / forward 1 frame** — by default these **stay paused** after a click. `step_button_resume_after_click = true` makes them resume at the pre-click rate instead.
  * **Clip-start hold** — `hold_paused_at_clip_start` (default true) overrides both of the above: when a jog or step lands at the start of the clip, the video holds paused regardless.
* All referee-window transport — the buttons above **and** the inertia wheel — is disabled (greyed out) whenever no challenge is active. The transport becomes interactive the moment the operator presses Challenge, and goes grey again when the challenge ends (operator presses Next Play / Time-out / End Game). No slider or progress bar appears on either window — the inertia wheel is the only scrubbing surface.
* **No chrome overlays the video feeds** on either window — no playback-status pill, resolution readout, freeze badge, or floating counters. Status that the operator/referee needs is shown outside the video panel (control areas, button labels, the diagnostic status bar).

