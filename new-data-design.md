- `<project-name>/`
    - `repos.txt`: list of git URLs for cloning
    - `issues.xml.gz`: compressed xml downloaded from issue tracker
    - ! `git.tar.xz`: compressed `.git/` of the repository used
    - `issue2git.csv`: contains a `(issue,sha)` mapping for the entire history

                issue: issue id extracted from the commit message
                sha: commit sha

    - `queries/`
        - `short/`
            - `<id>.txt`: UTF-8 of issue summary/title as extracted from the XML
        - `long/`
            - `<id>.txt`: UTF-8 of issue description as extracted from the XML
    - `changes-<level>.log`: contains changed source code entities in the format
      of `git log --name-status --pretty=raw`. When `<level>=file`, the output
      should be the same as actually running the command. When `<level>=class`
      or `<level>=method`, then the output will have the parsed entity names
      instead of file paths.
    - `<version>/`
        - `ids.txt`: a list of issue ids fixed in this version
        - ! `...`: any files generated. models, corpora, and results.


*Items marked with a bang (!) are not checked-in this repository, but should be
downloadable separately*
