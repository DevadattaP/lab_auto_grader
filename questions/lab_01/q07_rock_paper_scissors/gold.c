#include <stdio.h>

int main() {
    char p1, p2;

    scanf(" %c", &p1);
    scanf(" %c", &p2);

    switch (p1) {
        case 'R':
            switch (p2) {
                case 'R': printf("DRAW"); break;
                case 'P': printf("PLAYER 2 WINS"); break;
                case 'S': printf("PLAYER 1 WINS"); break;
                default: printf("INVALID INPUT");
            }
            break;

        case 'P':
            switch (p2) {
                case 'R': printf("PLAYER 1 WINS"); break;
                case 'P': printf("DRAW"); break;
                case 'S': printf("PLAYER 2 WINS"); break;
                default: printf("INVALID INPUT");
            }
            break;

        case 'S':
            switch (p2) {
                case 'R': printf("PLAYER 2 WINS"); break;
                case 'P': printf("PLAYER 1 WINS"); break;
                case 'S': printf("DRAW"); break;
                default: printf("INVALID INPUT");
            }
            break;
        
        default: printf("INVALID INPUT");
    }

    return 0;
}