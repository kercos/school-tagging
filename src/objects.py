from google.appengine.ext import ndb
from google.appengine.api import memcache
from google.appengine.api import channel
import json
import random
import re
import codecs
import string
import datetime
import logging

MAX_IDLE_ALLOWED = 100 # minutes
DEFAULT_LANGUAGE = "IT"

class decoder(json.JSONDecoder):
     # http://stackoverflow.com/questions/10885238/python-change-list-type-for-json-decoding
     def __init__(self, list_type=list, **kwargs):
          json.JSONDecoder.__init__(self, **kwargs)
          # Use the custom JSONArray
          self.parse_array = self.JSONArray
          # Use the python implemenation of the scanner
          self.scan_once = json.scanner.py_make_scanner(self)
          self.list_type = list_type

     def JSONArray(self, s_and_end, scan_once, **kwargs):
          values, end = json.decoder.JSONArray(s_and_end, scan_once, **kwargs)
          return self.list_type(values), end

class JsonSetEncoder(json.JSONEncoder):
     def default(self, obj):  # pylint: disable=method-hidden
          if isinstance(obj, frozenset):
                result = list(obj)
                if result and isinstance(result[0], tuple) and len(result[0]) == 2:
                     return dict(result)
                return result
          return json.JSONEncoder.default(self, obj)

def itemset(d):
     return frozenset(d.items())

def cleanIdleObjects():
     q = Teacher.query(Teacher.currentLessonID != None)
     if q.count(limit=None) > 0:
          teachers = q.fetch(limit=None)
          for teacher in teachers:
                idle = datetime.datetime.now() - teacher.lastAction
                if idle > datetime.timedelta(minutes=MAX_IDLE_ALLOWED):
                     lesson = getLesson(teacher.currentLessonID)
                     if lesson:
                          lesson.end()
                     teacher.logout()
     q = Student.query(Student.currentLessonID != None)
     if q.count(limit=None) > 0:
          students = q.fetch(limit=None)
          for student in students:
                idle = datetime.datetime.now() - teacher.lastAction
                if idle > datetime.timedelta(minutes=MAX_IDLE_ALLOWED):
                     student.logout()
     q = Lesson.query(Lesson.open == True)
     if q.count(limit=None) > 0:
          lessons = q.fetch(limit=None)
          for lesson in lessons:
                teacher = getTeacher(lesson.teacher)
                if teacher and teacher.currentLessonID == None:
                     lesson.end()
     q = Session.query(Session.open == True)
     if q.count(limit=None) > 0:
          sessions = q.fetch(limit=None)
          for session in sessions:
                teacher = getTeacher(session.teacher)
                if teacher and teacher.currentLessonID == None:
                     session.end()

class Answer(ndb.Model):
     session = ndb.IntegerProperty()
     content = ndb.StringProperty()
     correct = ndb.BooleanProperty()
     
class User(ndb.Model):
     fullname = ndb.StringProperty()
     username = ndb.StringProperty()
     currentLessonID = ndb.IntegerProperty()
     currentLessonName = ndb.StringProperty()
     lessons = ndb.IntegerProperty(repeated=True)
     token = ndb.StringProperty()
     currentSession = ndb.IntegerProperty()
     lastAction = ndb.DateTimeProperty(auto_now=True)
     language = ndb.StringProperty()
     newanswers = ndb.StructuredProperty(Answer, repeated=True)

     def connect(self):
          duration = 60 # minutes
          self.token = channel.create_channel(str(self.key.id()),
                                                     duration_minutes=duration)
          self.save()
     
     def assignLesson(self, lessonID, lessonName):
          self.currentLessonID = lessonID
          self.currentLessonName = lessonName
          self.lessons.append(lessonID)
          self.save()
          
     def askMeToRefresh(self):
          message = json.dumps({"type": "askMeRefresh"})
          channel.send_message(self.token, message)

class Student(User):
     answers = ndb.PickleProperty()
#            {sessionID1: answer, sessionID2: answer}
     def produceOwnStats(self):
          statsDict = {"correct": 0, "wrong": 0, "missing": 0}
          sessions = [s["session"] for s in self.answers]
          for sessionID in sessions:
              session = getSession(sessionID)
              if session:
                  for sa in self.answers:
                      if sa["session"] == sessionID:
                          answer = sa["answer"]
                          if answer == "MISSING":
                              statsDict["missing"] += 1
                          elif answer == session.validatedAnswer:
                              statsDict["correct"] += 1
                          elif answer != session.validatedAnswer:
                              statsDict["wrong"] += 1
          return statsDict

     def produceAndSendOwnStats(self):
                statsDict = self.produceOwnStats()
                message = {"type": "studentStats",
                          "message": {"stats": statsDict, "student": self.username}}
                self.sendMessageToTeacher(message)
     
     def save(self):
          if self.currentLessonID == None:
                cl = "Empty"
          else:
                cl = self.currentLessonID
          r = self.put()
          memcache.flush_all()
          memcache.set("Student:" + self.username + \
                          "|CurrentLesson:" + str(cl), self)
          return r
          
     def joinLesson(self, lessonName):
          assert lessonName in getOpenLessonsNames()
          lesson = getLessonFromName(lessonName)
          lesson.addStudent(self)
          self.currentLessonID = lesson.key.id()
          self.currentLessonName = lessonName
          self.answers = []
          self.save()
          self.alertTeacherImArrived()
          return lesson.key.id()
     
     def exitLesson(self):
          if self.currentLessonID:
              lesson = getLesson(self.currentLessonID)
              if lesson:
                  lesson.removeStudent(self)
                  message = {"type": "lessonTerminated"}
                  message = json.dumps(message)
                  channel.send_message(self.token, message)
                  self.currentLessonID = None
                  self.currentLessonName = None
                  self.save()
     
     def exitSession(self):
          if self.currentSession:
                session = getSession(self.currentSession)
                session.removeStudent(self)
                self.currentSession = None
                self.save()
     
     def logout(self):
          self.alertTeacherImLogout()
          self.exitLesson()
          self.exitSession()
          self.token = None
          self.save()
                
     def addAnswer(self, answer):
          try:
              sortedAnswer = json.loads(answer, cls=decoder, list_type=frozenset, object_hook=itemset)
              answer = json.dumps(sortedAnswer, cls=JsonSetEncoder)
          except:
              answer = answer
          session = getSession(self.currentSession)
          if session.open:
              self.answers.append(
                          {"session": self.currentSession, "answer": answer}
                     )
              self.save()
          
     def sendMessageToTeacher(self, message):
          lesson = getLesson(self.currentLessonID)
          if lesson and lesson.teacher:
                teacher = getTeacher(lesson.teacher)
                if teacher and teacher.token:
                  message = json.dumps(message)
                  return channel.send_message(teacher.token, message)
     
     def alertTeacherImArrived(self):
          message = {"type": "studentArrived",
                "message": {
                     "studentName": self.username,
                     "studentFullName": self.fullname
                     }}
          self.sendMessageToTeacher(message)
          
     def alertTeacherImLogout(self):
          message = {
                "type": "studentLogout",
                "message": {"studentName": self.username}
                }
          return self.sendMessageToTeacher(message)
     
     def alertTeacherImAlive(self):
          message = {"type": "studentAlive",
                "message": {"studentName": self.username}}
          self.sendMessageToTeacher(message)
     
     def alertTeacherImOffline(self):
          message = {"type": "studentDisconnected",
                "message": {"studentName": self.username}}
          self.sendMessageToTeacher(message)

     def alertTeacherAboutMyFocus(self, status):
          message = {"type": "studentFocusStatus",
                "message": {"studentName": self.username, "focus": status}}
          self.sendMessageToTeacher(message)

class Teacher(User):
     password = ndb.StringProperty()
     def save(self):
          self.put()
          memcache.set("Teacher:" + self.username, self)
          
     def logout(self):
          self.currentLessonID = None
          self.currentLessonName = None
          self.currentSession = None
          self.token = None
          self.save()
          
     def sendPingToStudent(self, studentName):
          student = getStudent(studentName, self.currentLessonID)
          message = {"type": "pingFromTeacher"}
          message = json.dumps(message)
          channel.send_message(student.token, message)

def teacherUsernameExists(username):
     if getTeacher(username):
          return True
     else:
          return False

def createTeacher(username, password, fullname):
     teacher = Teacher()
     teacher.username = username
     teacher.fullname = fullname
     teacher.password = password
     teacher.language = DEFAULT_LANGUAGE
     teacher.save()
     return

def getTeacher(username):
     teacher = memcache.get("Teacher:" + username)
     if not teacher:
          q = Teacher.query(Teacher.username == username)
          teacher = q.get()
          if teacher:
                memcache.set("Teacher:" + username, teacher)
     if teacher:
          return teacher
     else:
          return False

def getStudent(username, currentLessonID):
     student = memcache.get("Student:" + username + \
                "|CurrentLesson:" + str(currentLessonID))
     if not student:
          q = Student.query(Student.username == username,
                Student.currentLessonID == currentLessonID)
          student = q.get()
          if student:
                memcache.set("Student:" + username + \
                     "|CurrentLesson:" + str(currentLessonID), student)
     if student:
          return student
     else:
          return False
          
def getFromID(sid):
     user = memcache.get("ID:" + sid)
     if not user:
          user = ndb.Key("Teacher", int(sid)).get() \
                or ndb.Key("Student", int(sid)).get()
          #~ user = ndb.get_by_id(int(id))
          if user:
                memcache.set("ID:" + str(sid), user)
     if user:
          return user
     else:
          return False
     
def studentAlreadyConnected(username, lessonName):
     q = Student.query(Student.username == username,
          Student.currentLessonName == lessonName)
     if q.get():
          return True
     else:
          return False
     
class Lesson(ndb.Model):
     lessonName = ndb.StringProperty()
     teacher = ndb.StringProperty()
     open = ndb.BooleanProperty()
     sessions = ndb.IntegerProperty(repeated=True)
     students = ndb.StringProperty(repeated=True)
     datetime = ndb.DateTimeProperty(auto_now_add=True)

     def start(self, lessonName, teacher):
          self.lessonName = lessonName
          self.teacher = teacher.username
          self.open = True
          self.students = []
          self.sessions = []
          lessonID = self.save()
          teacher.assignLesson(lessonID, lessonName)

     def save(self):
          self.put()
          memcache.set("Lesson:" + str(self.key.id()), self)
          return self.key.id()

     def end(self):
          teacher = getTeacher(self.teacher)
          for studentName in self.students:
                student = getStudent(studentName, self.key.id())
                student.exitLesson()
          teacher.currentLessonID = None
          teacher.currentLessonName = None
          teacher.save()
          self.open = False
          self.save()
          
     def addStudent(self, student):
          self.students.append(student.username)
          self.save()
     
     def addSession(self, sessionID):
          self.sessions.append(sessionID)
          self.save()
     
     def removeStudent(self, student):
          if student.username in self.students:
                self.students.remove(student.username)
                self.save()
     
     def produceAndSendStats(self):
          teacher = getTeacher(self.teacher)
          allStudents = []
          listOfStudentsStats = []
          stats = None
          if self.sessions:
              stats = []
              for sessionID in self.sessions:
                  ses = getSession(sessionID)
                  if ses:
                      students = ses.studentAnswers.keys()
                      allStudents += self.students
                      if students:
                          for st in students:
                              alreadyTracked = [s["studentName"] for s in listOfStudentsStats]
                              if st not in alreadyTracked:
                                  student = getStudent(st, self.key.id())
                                  if student:
                                      ownStats = student.produceOwnStats()          
                                      ownDict = {"studentName": st, "stats": ownStats}
                                      listOfStudentsStats.append(ownDict) 
                          corrects = [st for st in students \
                              if ses.studentAnswers[st] == ses.validatedAnswer]
                          stats += corrects
          statsDict = {}
          if stats:
              for name in stats:
                  if name in statsDict.keys():
                      statsDict[name] += 1
                  else:
                      statsDict[name] = 1
          for student in students:
              if student not in statsDict.keys():
                  statsDict[student]= 0
          message = {
          "type": "lessonStats",
          "message": {
                "stats": statsDict,
                "fullstats": listOfStudentsStats
          }
          }
          message = json.dumps(message)
          channel.send_message(teacher.token, message)
     
def getOpenLessonsID():
     q = Lesson.query(Lesson.open == True)
     if q.count(limit=None) > 0:
          lessons = q.fetch(limit=None)
          return [lesson.key.id() for lesson in lessons]
     else:
          return []

def getOpenLessonsNames():
     q = Lesson.query(Lesson.open == True)
     if q.count(limit=None) > 0:
          lessons = q.fetch(limit=None)
          return [lesson.lessonName for lesson in lessons]
     else:
          return []
     
def getLesson(lessonID):
     lesson = memcache.get("Lesson:" + str(lessonID))
     if not lesson:
          lesson = ndb.Key("Lesson", lessonID).get()
          if lesson:
                memcache.set("Lesson:" + str(lessonID), lesson)
     if lesson:
          return lesson
     else:
          return False
     
def getLessonFromName(lessonName):
     q = Lesson.query(Lesson.lessonName == lessonName)
     lesson = q.get()
     if lesson:
          return lesson
     else:
          return False

def getSession(sessionID):
     session = memcache.get("Session:" + str(sessionID))
     if not session:
          session = ndb.Key("Session", sessionID).get()
          if session:
                memcache.set("Session:" + str(sessionID), session)
     if session:
          return session
     else:
          return False
     
def getAnswer(answerID):
     answer = memcache.get("Answer:" + str(answerID))
     if not answer:
          answer = ndb.Key("Answer", answerID).get()
          if answer:
                memcache.set("Answer:" + str(answerID), answer)
     if answer:
          return answer
     else:
          return False

def getSentence():
     pool = codecs.open("sentence-pool.txt", encoding="UTF-8").readlines()
     i = int(random.random() * len(pool))
     return pool[i]

def getWords(sentence):
     """return word list, random choice within words."""
     r = re.compile('[^%s]+' % re.escape(string.punctuation))
     raw_pool = re.split(' ', sentence)
     pool = [r.match(p) for p in raw_pool]
     goods = []
     words = []
     for p in range(len(pool)):
          if pool[p]:
                goods += [True]
                words += [pool[p].group()]
          else:
                goods += [False]
                words += [raw_pool[p]]
     target = int(random.random() * len(words))
     while not goods[target]:
          target = int(random.random() * len(words))
     return words, target
     
def getAnswersProposed(exerciseType):
     return json.loads(open("lists/answers.json","r").read())[exerciseType]

def clean():
     ndb.delete_multi(Lesson.query().fetch(keys_only=True))
     ndb.delete_multi(Session.query().fetch(keys_only=True))
     ndb.delete_multi(Student.query().fetch(keys_only=True))
     ndb.delete_multi(Teacher.query().fetch(keys_only=True))
     memcache.flush_all()


#     def save(self):
#          self.put()
#          memcache.set("Answer:" + str(self.key.id()) + "|Parent:" \
#                        + str(self.parent.key.id()), self)
#          return self.key.id()

class Session(ndb.Model):
     teacher = ndb.StringProperty()
     open = ndb.BooleanProperty()
     lesson = ndb.IntegerProperty()
     type = ndb.StringProperty()
     students = ndb.StringProperty(repeated=True)
     datetime = ndb.DateTimeProperty(auto_now_add=True)
     exerciseText = ndb.StringProperty()
#            sentence to be analized from the student
     target = ndb.IntegerProperty()
#            index of the word that the student should recognize
     answersProposed = ndb.PickleProperty(repeated=True)
#            options available for the student
     exerciseWords = ndb.StringProperty(repeated=True)
#            list of the words componing the exercise
     validatedAnswer = ndb.StringProperty()
#            index of the teacher's validated answer in the answersProposed list
     studentAnswers = ndb.PickleProperty()
#             {student1: answer1, student2: answer2}
     answersStudents = ndb.PickleProperty()
#            {answer1:[student1, student2], answer2:[], answer3:[student3]}
     
     def addStudentAnswer(self, studentName, answer):
          # TODO recasted, ready for cleaning (replace with ndb one)
          if self.open:
                self.studentAnswers[studentName] = answer
                if answer in self.answersStudents.keys():
                     self.answersStudents[answer].append(studentName)
                else:
                     self.answersStudents[answer] = [studentName]
                print "Add Student Answer:" + str(self.studentAnswers.keys())
                return self.save()
          return None
                     
     def addNdbAnswer(self, role, userName, answer):
         if role == "student" and not self.open:
             return None
         if role == "student" and self.open:
             student = getStudent(userName, self.lesson)
             student.newanswers.append( \
                       Answer(session=self.key.id(),content=answer))
             return student.save()
         elif role == "teacher":
             try:
                 sortedValid = json.loads(answer, cls=decoder, list_type=frozenset, object_hook=itemset)
                 valid = json.dumps(sortedValid, cls=JsonSetEncoder)
             except:
                 valid = answer
             teacher = getTeacher(self.teacher)
             teacher.newanswers.append( \
                       Answer(session=self.key.id(),content=answer,correct=True))
             return teacher.save()

     def addValidAnswer(self, validAnswer):
          # TODO recasted, ready for cleaning (replace with ndb one)
          try:
                sortedValid = json.loads(validAnswer, cls=decoder, list_type=frozenset, object_hook=itemset)
                valid = json.dumps(sortedValid, cls=JsonSetEncoder)
          except:
                valid = validAnswer
          self.validatedAnswer = valid
          self.save()
          
     def sendFeedbackToStudents(self):
          for studentName in self.students:
                student = getStudent(studentName, self.lesson)
                # TODO function modified, ready for recast
                myanswer = [a["answer"] for a in student.answers \
                          if a["session"] == self.key.id()]
                if myanswer and myanswer[0] != "MISSING":
                     myanswer = myanswer[0]
                     message = {
                     "type": "validAnswer",
                     "message": {
                          "validAnswer": self.validatedAnswer,
                          "myAnswer": myanswer,
                          "dict": getAnswersProposed(self.type)
                          }
                     }
                else:
                     message = {
                     "type": "sessionExpired",
                     "message": {
                          "validAnswer": self.validatedAnswer,
                          "dict": getAnswersProposed(self.type)
                     }
                }
                message = json.dumps(message)
                channel.send_message(student.token, message)
# new logic
                sessionAnswers = [a for a in student.newanswers if a.session == self.key.id()]
                if sessionAnswers and sessionAnswers[0] != "MISSING":
                    answer = sessionAnswers[0]
                    message = {
                    "type": "validAnswer",
                    "message": {
                         "validAnswer": self.validatedAnswer,
                         "myAnswer": unicode(answer.content),
                         "dict": getAnswersProposed(self.type)
                         }
                    }
                else:
                    message = {
                    "type": "sessionExpired",
                    "message": {
                         "validAnswer": self.validatedAnswer,
                         "dict": getAnswersProposed(self.type)
                    }
                }
#                logging.info("New feedback: %s", json.dumps(message))
          
     def save(self):
          self.put()
          memcache.set("Session:" + str(self.key.id()), self)
          return self.key.id()
          
     def sendStatusToTeacher(self):
          if self.open:
                teacher = getTeacher(self.teacher)
                if teacher:
                  # TODO ALMOST THERE remove the old status and leave only the new one. Ready for recast
                  status = {
                      "type": "sessionStatus",
                      "message": {
                          "dictAnswers": getAnswersProposed(self.type),
                          "possibleAnswers": self.answersStudents,
                          "totalAnswers": {
                         "answered": self.studentAnswers.keys(),
                         "missing": [s for s in self.students \
                             if s not in self.studentAnswers.keys()]
                          }
                          },
                      }
                answered = ""
                newstatus = {
                      "type": "sessionStatus",
                      "message": {
                          "dictAnswers": getAnswersProposed(self.type),
                          "possibleAnswers": self.generateAnswersDict("answerStudent"),
                          "totalAnswers": {
                            "answered": self.generateAnswersDict("studentAnswer").keys(),
                            "missing": [s for s in self.students \
                                if s not in self.generateAnswersDict("studentAnswer").keys()]
                          }
                          },
                      }
                channel.send_message(teacher.token, json.dumps(status))

     def generateAnswersDict(self, dicttype):
        # depending of the parameter, return a dict
		# "answerStudent": return {answer1:[student1, student2], answer2:[], answer3:[student3]}
		# "studentAnswer": return {student1: answer1, student2: answer2}
         answers = {}
         for studentName in self.students:
             student = getStudent(studentName, self.lesson)
             if student:
                 for objAnswer in student.newanswers:
                     if self.key.id() == objAnswer.session:
                         answer = objAnswer.content
                         if dicttype == "answerStudent":
                             if answer in answers.keys():
                                 answers[answer] += [studentName]
                             else:
                                 answers[answer] = [studentName]
                         elif dicttype == "studentAnswer":
                             if studentName in answers.keys():
                                 answers[studentName] += [answer]
                             else:
                                 answers[studentName] = [answer]
         return answers

     def removeStudent(self, student):
          if student.username in self.students:
                self.students.remove(student.username)
                self.save()
     
     def end(self):
          for studentName in self.students:
                student = getStudent(studentName, self.lesson)
                if student:
                     sessions = [s["session"] for s in student.answers]
                     if self.key.id() not in sessions:
                      a = {"session": self.key.id(), "answer": "MISSING"}
                      student.answers.append(a)
                      student.save()
# new logic TODO clean the part above
                     if self.key.id() not in [a.session for a in student.newanswers]:
                         student.newanswers.append( \
                               Answer(session=self.key.id(),content="MISSING"))
                         student.save() 
                self.open = False
                self.save()
          
     def start(self, lessonID, exerciseType,category=""):
          self.lesson = lessonID
          lesson = getLesson(lessonID)
          self.teacher = lesson.teacher
          self.students = lesson.students
          self.exerciseText = getSentence()
          self.type = exerciseType
          self.category = category
          if exerciseType == "complex" :
                self.exerciseWords, self.target = getWords(self.exerciseText)
                self.target = -1
                self.answersProposed = []
          else:
                self.exerciseWords, self.target = getWords(self.exerciseText)
                self.answersProposed = getAnswersProposed(self.type)
          # TODO modify here
          self.studentAnswers = {}
          self.answersStudents = {}
          self.open = True
          sid = self.save()
          lesson.addSession(sid)
          teacher = getTeacher(self.teacher)
          teacher.currentSession = self.key.id()
          teacher.save()
          message = {
                "type": "sessionExercise",
                "message": {
                "id": sid,
                "wordsList": self.exerciseWords,
                "answersProposed": self.answersProposed,
                "target": self.target,
                "category": self.category
                },
                }
          
          message = json.dumps(message)
          channel.send_message(teacher.token, message)
          for studentName in self.students:
                student = getStudent(studentName, self.lesson)
                if student:
                    student.currentSession = sid
                    student.save()
                    channel.send_message(student.token, message)
          self.sendStatusToTeacher()

def exportJson():
     j = None
     q = Lesson.query()
     if q.count(limit=None) > 0:
          j = [q.fetch(limit=None)]
     q = Teacher.query()
     if q.count(limit=None) > 0:
          j += [q.fetch(limit=None)]
     q = Student.query()
     if q.count(limit=None) > 0:
          j += [q.fetch(limit=None)]
     q = Session.query()
     if q.count(limit=None) > 0:
          j += [q.fetch(limit=None)]
     return j
